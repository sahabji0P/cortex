"""
The loop a gem runs to make one reply, whatever the reply is delivered on.

    model round ──▶ tool calls ──▶ their results ──▶ next model round ──▶ …

A reply is one or more model rounds. Each round is sent the history. A round that calls
tools is followed by another once their results are in; the reply ends with a round
that only says something. What a round produced is final once the next round is sent
it: the conversation adds it to its history then (`Reply.send_back`), so the history
is always everything the model has been sent, in the order it was sent.

What the model says goes to a `Surface`: the place a reply is delivered, such as speech
in a call or text in writing. The loop knows a surface only by the small protocol below.
It never asks how the text is delivered, only how much of it got through by a given
moment. That is what the history keeps, by the same rules everywhere:

- The history holds what was delivered, not what was generated. A reply cut short keeps
  its delivered words, ending in "…".
- Nothing the model has been sent is changed afterwards. So text that was sent back to
  the model, in a later round of the same reply, is kept in full however much of it was
  delivered. (Only at a cold start, between replies, are old long tool results sent
  expired: see `cold_start.py`.)
- Every tool call keeps a valid result. A call still running at a cut is left running
  with a placeholder as its result, the same in a call and in writing (see
  `late_results.py`).

The history, whether the model thinks first, and where the text and tool events go are
the conversation's own. It hands them over at the start of each round.
"""

import asyncio
import contextlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, NamedTuple, Protocol

from app.entities.model import Item, Model, ModelCall, RoundDone, TextDelta, Usage
from app.entities.tools import Tool, Toolbox

__all__ = [
    "CUT_MARK",
    "Delivered",
    "Part",
    "Record",
    "Reply",
    "RequestRecord",
    "ResultMark",
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


# What a call's result stands for, beyond its text: the placeholder a call left running
# was given, or the answer of a `cancel_tool_call` that stopped such a call. It is kept
# with the result, so that a result is never recognised by its wording.
type ResultMark = Literal["placeholder", "cancelled"]


@dataclass(slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: str
    # The tool called, or None if the conversation has no tool of that name.
    tool: Tool | None = None
    output: str | None = None  # None while the tool is still running
    running: asyncio.Task[str] | None = None
    mark: ResultMark | None = None
    # Whether the tool failed, once it has run. The model is told so in the result's
    # own words: this is for whoever writes down what happened.
    failed: bool = False

    def items(self) -> list[Item]:
        return [
            {
                "type": "function_call",
                "call_id": self.call_id,
                "name": self.name,
                "arguments": self.arguments,
            },
            {"type": "function_call_output", "call_id": self.call_id, "output": self.output or ""},
        ]


@dataclass(slots=True)
class Delivered:
    """A reply as it was delivered."""

    # What joins the history now: what was delivered of the parts that have not joined
    # it already, when they were sent back to the model.
    items: list[Item]
    any_text: bool  # whether any of the reply's text was delivered
    kept: str | None  # the text kept of a reply that was cut short; None if delivered in full
    # Every stretch of the reply's text that the history keeps, in order: those that
    # joined it earlier as well.
    texts: list[str]


class Reply:
    """One reply while it is produced and delivered: what was said and what was called."""

    def __init__(self, surface: Surface) -> None:
        self.surface = surface
        # Whether it was produced to the end, as opposed to cut off or failed.
        self.finished = False
        self._parts: list[Part | ToolCall] = []
        # How many of the parts have been sent back to the model, in a later call of
        # this same reply. Those are final: the model has seen them as they are, and
        # they are in the history.
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
        """What the reply has produced since this was last asked, which is final from here.

        The reply's own next model call is about to be sent it, so it joins the history
        as it stands: the text in full, and each call with its result.
        """
        items: list[Item] = []
        for part in self._parts[self._sent_back :]:
            if isinstance(part, ToolCall):
                items.extend(part.items())
            elif text := part.text.strip():
                items.append({"role": "assistant", "content": text})
        self._sent_back = len(self._parts)
        return items

    def delivered(self, at: float) -> Delivered:
        """The reply as it had been delivered by time `at`.

        Two things are kept whatever was delivered:

        - Tool calls: they happened, and the history has to stay a valid chain of calls
          and results.
        - What was already sent back to the model: the text it said before calling a
          tool, and the calls. That is in the history as it stands. Shortening the text
          now would change something the model has been shown, and with it everything
          a prompt cache holds from there on.
        """
        items: list[Item] = []
        texts: list[str] = []
        cut = False
        any_text = False
        for index, part in enumerate(self._parts):
            final = index < self._sent_back  # in the history already
            if isinstance(part, ToolCall):
                if not final:
                    items.extend(part.items())
                continue
            if cut and not final:
                continue  # produced after delivery stopped
            text, whole = part.delivered(at)
            any_text = any_text or bool(text)
            if final:
                text = part.text.strip()
            elif not whole:
                text = f"{text}{CUT_MARK}" if text else ""
            cut = cut or not whole
            if text:
                texts.append(text)
                if not final:
                    items.append({"role": "assistant", "content": text})
        return Delivered(items, any_text, " ".join(texts) if cut else None, texts)


class RequestRecord(Protocol):
    """Where what happens to one request to the model is written down."""

    def first(self) -> None:
        """The first of the model's output has come."""
        ...

    def done(self, text: str, calls: Sequence[ModelCall], usage: Usage | None) -> None:
        """The model finished: all the text it produced, its tool calls, and what it used."""
        ...

    def stopped(self, text: str, error: BaseException) -> None:
        """The request ended before the model finished, with the text produced until then."""
        ...


class Record(Protocol):
    """Where what a reply does is written down (see `gem_log.py`). None of it may raise."""

    def sent(self, items: Sequence[Item], think: bool) -> RequestRecord:
        """A request is about to be sent these items."""
        ...

    def tool(self, call: ToolCall) -> None:
        """A tool call has started running."""
        ...


class _NoRequestRecord:
    def first(self) -> None:
        pass

    def done(self, text: str, calls: Sequence[ModelCall], usage: Usage | None) -> None:
        pass

    def stopped(self, text: str, error: BaseException) -> None:
        pass


class _NoRecord:
    """Writes nothing down."""

    def sent(self, items: Sequence[Item], think: bool) -> RequestRecord:
        return _NoRequestRecord()

    def tool(self, call: ToolCall) -> None:
        pass


class RoundInput(NamedTuple):
    """What a conversation gives one model round, read when the round starts."""

    # The history as it stands now, with what this reply has produced so far: the
    # conversation adds that first (see `Reply.send_back`).
    items: list[Item]
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
    record: Record | None = None,
) -> None:
    """Produce one reply: model rounds, with tool calls in between, delivered as they stream.

    `next_round` is asked at the start of every round, not once per reply: what it
    returns can change while tools run, and it is where the conversation adds what the
    reply has produced so far to its history. `said` is given each piece of text once the
    surface has taken it, and `show` the text of each tool call's `tool.call` event.
    `record` is where each request and tool call is written down, if anywhere: it
    changes nothing of what the model is sent or what the reply does.

    The surface is finished when the reply is, and stopped if it is cut off or fails.
    A failure is raised once the surface has stopped.
    """
    record = _NoRecord() if record is None else record
    try:
        # The model calls what it needs: there is no cap on how many rounds of tool
        # calls one reply may make. It ends with a round that makes none.
        while await _model_round(reply, model, tools, instructions, next_round, said, show, record):
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
    record: Record,
) -> bool:
    """Run one model call, delivering its text. Return True if it called tools (so: again)."""
    before = next_round()
    items = before.items
    request = record.sent(items, before.think)
    events = model.round(items, instructions=instructions, think=before.think)
    calls: list[ToolCall] = []
    produced: list[str] = []
    done: RoundDone | None = None
    try:
        # Closed on the way out whatever happens, so the model call never outlives this.
        async with contextlib.aclosing(events):
            async for event in events:
                if not isinstance(event, RoundDone):
                    request.first()
                if isinstance(event, TextDelta):
                    produced.append(event.text)
                    await reply.say(event.text)
                    said(event.text)
                elif isinstance(event, RoundDone):
                    done = event
                    calls = [
                        ToolCall(call.call_id, call.name, call.arguments, tool=tools.get(call.name))
                        for call in event.calls
                    ]
    except BaseException as stopped:
        # It failed, or the reply was cut off while the model was still producing.
        request.stopped("".join(produced), stopped)
        raise
    request.done(
        "".join(produced),
        [] if done is None else done.calls,
        None if done is None else done.usage,
    )
    if not calls:
        return False
    # Whatever was said before calling tools ("Let me check") is complete as it stands.
    await reply.surface.end_part()
    # Each call runs as its own task, so that cutting the reply off does not stop it.
    for call in calls:
        reply.add_call(call)
        call.running = asyncio.create_task(_run(tools, call, show))
        record.tool(call)
    for call in calls:
        if call.running is not None:
            call.output = await asyncio.shield(call.running)
    return True


async def _run(tools: Toolbox, call: ToolCall, show: Callable[[str], None]) -> str:
    """Run one tool call to its result for the model, noting on it whether the tool failed."""
    result, call.failed = await tools.attempt(call.name, call.arguments, show=show)
    return result
