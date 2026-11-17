"""
The loop a gem runs to make one reply, whatever the reply is delivered on.

    model round ──▶ tool calls ──▶ their results ──▶ next model round ──▶ …

A reply is one or more model rounds. Each round is sent the history and whatever the
reply has produced so far. A round that calls tools is followed by another once their
results are in; the reply ends with a round that only says something.

What the model says goes to a `Surface`: the place a reply is delivered, such as speech
in a call or text in writing. The loop knows a surface only by the small protocol below.
It never asks how the text is delivered, only how much of it got through by a given
moment. That is what the history keeps, by the same rules everywhere:

- The history holds what was delivered, not what was generated. A reply cut short keeps
  its delivered words, ending in "…".
- Nothing the model has been sent is changed afterwards. So text that was sent back to
  the model, in a later round of the same reply, is kept in full however much of it was
  delivered.
- Every tool call keeps a valid result. What a call still running at a cut gets as its
  result is decided by the conversation that made it.

Which history a round is sent, whether the model thinks first, and where the text and
tool events go are the conversation's own. It hands them over at the start of each round.
"""

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import NamedTuple, Protocol

from app.entities.model import CallResult, Item, Model, RoundDone, TextDelta
from app.entities.tools import Tool, Toolbox

__all__ = [
    "CUT_MARK",
    "Delivered",
    "Part",
    "Reply",
    "RoundInput",
    "Surface",
    "ToolCall",
    "run_reply",
]

# Ends the stored text of a reply that was cut off.
CUT_MARK = "…"


class Part(Protocol):
    """One stretch of a reply's text, as it is delivered."""

    @property
    def text(self) -> str:
        """Everything said in it, whether or not it has been delivered."""
        ...

    def delivered(self, at: float) -> tuple[str, bool]:
        """What of it had been delivered by time `at`, and whether that is all of it."""
        ...


class Surface(Protocol):
    """Where one reply is delivered. Each reply has its own."""

    async def say(self, text: str) -> Part:
        """Deliver the next piece of text. Return the part it became part of."""
        ...

    async def end_part(self) -> None:
        """No more text for the current part. The next text starts a new one."""
        ...

    async def finish(self) -> None:
        """No more text at all. Return once everything has been delivered."""
        ...

    async def stop(self) -> None:
        """Stop delivering, from this instant, and release what the surface holds."""
        ...


@dataclass(slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: str
    # The tool called, or None if the conversation has no tool of that name.
    tool: Tool | None = None
    output: str | None = None  # None while the tool is still running
    running: asyncio.Task[str] | None = None
    # Whether `output` is a placeholder standing in for a result that comes later.
    placeholder: bool = False
    # Whether the result has been sent back to the model, in a later call of its reply.
    seen: bool = False
    # The result item most recently built by `items()`: the one in the history, once
    # this call is there.
    result_item: CallResult | None = None

    def items(self) -> list[Item]:
        self.result_item = {
            "type": "function_call_output",
            "call_id": self.call_id,
            "output": self.output or "",
        }
        return [
            {
                "type": "function_call",
                "call_id": self.call_id,
                "name": self.name,
                "arguments": self.arguments,
            },
            self.result_item,
        ]

    @property
    def clearable_result(self) -> CallResult | None:
        """The item holding this call's result, if it is a real one that may be cleared."""
        clearable = self.tool is not None and self.tool.clearable
        return self.result_item if clearable and not self.placeholder else None


@dataclass(slots=True)
class Delivered:
    """A reply as it was delivered."""

    items: list[Item]  # what joins the history
    any_text: bool  # whether any of the reply's text was delivered
    kept: str | None  # the text kept of a reply that was cut short; None if delivered in full
    undelivered: str  # the text that was dropped


class Reply:
    """One reply while it is produced and delivered: what was said and what was called."""

    def __init__(self, surface: Surface) -> None:
        self.surface = surface
        # Whether it was produced to the end, as opposed to cut off or failed.
        self.finished = False
        self._parts: list[Part | ToolCall] = []
        # How many of the parts have been sent back to the model, in a later call of
        # this same reply. Those are final: the model has seen them as they are.
        self._sent_back = 0

    async def say(self, text: str) -> None:
        part = await self.surface.say(text)
        if not self._parts or self._parts[-1] is not part:
            self._parts.append(part)

    def add_call(self, call: ToolCall) -> None:
        self._parts.append(call)

    def calls(self) -> list[ToolCall]:
        return [part for part in self._parts if isinstance(part, ToolCall)]

    def send_back(self) -> list[Item]:
        """Everything produced so far, for this reply's own next model call."""
        self._sent_back = len(self._parts)
        items: list[Item] = []
        for part in self._parts:
            if isinstance(part, ToolCall):
                part.seen = True
                items.extend(part.items())
            elif text := part.text.strip():
                items.append({"role": "assistant", "content": text})
        return items

    def delivered(self, at: float) -> Delivered:
        """The reply as it had been delivered by time `at`.

        Two things are kept whatever was delivered:

        - Tool calls: they happened, and the history has to stay a valid chain of calls
          and results.
        - Text already sent back to the model, which is what it said before calling a
          tool. Shortening it now would change something the model has been shown, and
          with it everything a prompt cache holds from there on.
        """
        items: list[Item] = []
        kept: list[str] = []
        undelivered: list[str] = []
        cut = False
        any_text = False
        for index, part in enumerate(self._parts):
            if isinstance(part, ToolCall):
                items.extend(part.items())
                continue
            said = part.text.strip()
            if cut and index >= self._sent_back:
                undelivered.append(said)  # produced after delivery stopped
                continue
            text, whole = part.delivered(at)
            any_text = any_text or bool(text)
            if index < self._sent_back:
                text = said
            elif not whole:
                undelivered.append(said[len(text) :].strip())
                text = f"{text}{CUT_MARK}" if text else ""
            cut = cut or not whole
            if text:
                kept.append(text)
                items.append({"role": "assistant", "content": text})
        return Delivered(items, any_text, " ".join(kept) if cut else None, " ".join(undelivered))


class RoundInput(NamedTuple):
    """What a conversation gives one model round, read when the round starts."""

    items: list[Item]  # everything before this reply, as it stands now
    think: bool  # whether the model is asked to think a little first


async def run_reply(
    reply: Reply,
    model: Model,
    tools: Toolbox,
    *,
    instructions: str,
    next_round: Callable[[], RoundInput],
    said: Callable[[str], None],
    show: Callable[[str], None],
) -> None:
    """Produce one reply: model rounds, with tool calls in between, delivered as they stream.

    `next_round` is asked at the start of every round, not once per reply: what it
    returns can change while tools run. `said` is given each piece of text once the
    surface has taken it, and `show` the text of each tool call's `tool.call` event.

    The surface is finished when the reply is, and stopped if it is cut off or fails.
    A failure is raised once the surface has stopped.
    """
    try:
        # The model calls what it needs: there is no cap on how many rounds of tool
        # calls one reply may make. It ends with a round that makes none.
        while await _model_round(reply, model, tools, instructions, next_round, said, show):
            pass
        await reply.surface.finish()
    except (asyncio.CancelledError, Exception):
        await reply.surface.stop()
        raise
    reply.finished = True


async def _model_round(
    reply: Reply,
    model: Model,
    tools: Toolbox,
    instructions: str,
    next_round: Callable[[], RoundInput],
    said: Callable[[str], None],
    show: Callable[[str], None],
) -> bool:
    """Run one model call, delivering its text. Return True if it called tools (so: again)."""
    before = next_round()
    events = model.round(
        [*before.items, *reply.send_back()], instructions=instructions, think=before.think
    )
    calls: list[ToolCall] = []
    # Closed on the way out whatever happens, so the model call never outlives this.
    async with contextlib.aclosing(events):
        async for event in events:
            if isinstance(event, TextDelta):
                await reply.say(event.text)
                said(event.text)
            elif isinstance(event, RoundDone):
                calls = [
                    ToolCall(call.call_id, call.name, call.arguments, tool=tools.get(call.name))
                    for call in event.calls
                ]
    if not calls:
        return False
    # Whatever was said before calling tools ("Let me check") is complete as it stands.
    await reply.surface.end_part()
    # Each call runs as its own task, so that cutting the reply off does not stop it.
    for call in calls:
        reply.add_call(call)
        call.running = asyncio.create_task(tools.run(call.name, call.arguments, show=show))
    for call in calls:
        if call.running is not None:
            call.output = await asyncio.shield(call.running)
    return True
