"""
Written entity: a conversation with a gem in writing.

One message in, one reply out. The reply is made by the same loop as a call's (see
`loop.py`) and delivered as text: each piece reaches the reader as an event of the
stream `send` returns. A reply can also be written with no new message, as when a late
search result is to be told.

A conversation is given the history to start from, and hands back what it added to it
(`added`): the user's message, the reply as it was delivered, and its tool calls with
their results. Keeping those is the caller's job. Chat makes one of these for every
message, from what is stored (see `chat_ent.py`).

The history follows the same rules as a call's. In writing, what was delivered is what
the reader was handed, and the reply runs at most one piece of text ahead of them. A
reply that does not finish, because it was cut, the reader stopped part-way (closed the
stream, or was cancelled), or it failed, leaves the history with the text the reader was
handed, ending in "…". Nothing is made up to mark the cut: a reply cut before any of
it was handed over leaves no reply at all, as a call's does, and one cut while a tool
ran keeps the text before the call whole.

A tool call still running at a cut is not stopped, as in a call. It is left to the
conversation's `LateResults`, given by the caller, which holds it until its result is
told: telling it is the caller's job (see `late_results.py`). Its result in the history
is the placeholder, and the calls this conversation left running are in `left_running`.
"""

import asyncio
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from app.entities.late_results import LateResults
from app.entities.loop import CUT_MARK, Reply, RoundInput, ToolCall, run_reply
from app.entities.model import Item, Model
from app.entities.tools import Toolbox

__all__ = ["Added", "WrittenConversation", "WrittenEvent", "shown"]


@dataclass(frozen=True, slots=True)
class WrittenEvent:
    """One thing that happens in a written reply.

    - `text`       the next piece of the reply's text
    - `tool.call`  a tool call is being made (what it does in `text`, such as a query)
    - `done`       the reply is complete. `text` is the whole reply
    - `cut`        the reply was cut off. `text` is what is kept of it: ending in "…" if
                   it was cut mid-text, whole if it was cut while a tool ran (the tool
                   carries on), and just "…" if nothing is kept

    The text of `done` and `cut` is the reply's stretches of text, as the history keeps
    them, joined by a space.
    """

    type: Literal["text", "tool.call", "done", "cut"]
    text: str = ""


@dataclass(frozen=True, slots=True)
class Added:
    """One item a conversation added to its history."""

    item: Item
    # When it was added. The user's message carries when it was sent.
    at: datetime
    # For a tool call, the text of its `tool.call` event, if it had one.
    shown: str | None = None


class _Passage:
    """One stretch of a written reply: its text, and how much of it the reader has."""

    def __init__(self) -> None:
        self.text = ""
        self.handed = 0  # how many characters of it the reader has been handed
        self.ended = False  # whether more text can join it

    def delivered(self, at: float) -> tuple[str, bool]:
        # Time plays no part in writing: what the reader was handed is delivered.
        return self.text[: self.handed].strip(), self.ended and self.handed == len(self.text)


# One event for the reader, the passage its text belongs to, and what to tell once the
# reader has it. None ends the stream.
type _Handed = tuple[WrittenEvent, _Passage | None, asyncio.Future[None] | None] | None


class _Page:
    """The surface a written reply is delivered on.

    Each piece of text waits for the reader to take the one before it, and a passage
    is only ended once the reader has all of it. So nothing is sent back to the model,
    or followed by a tool call, before the reader has it.
    """

    def __init__(self, out: asyncio.Queue[_Handed]) -> None:
        self._out = out
        self._passage: _Passage | None = None
        self._taken: asyncio.Future[None] | None = None

    async def say(self, text: str) -> _Passage:
        await self._handed_over()
        if self._passage is None or self._passage.ended:
            self._passage = _Passage()
        self._passage.text += text
        self._taken = asyncio.get_running_loop().create_future()
        self._out.put_nowait((WrittenEvent("text", text), self._passage, self._taken))
        return self._passage

    async def end_part(self) -> None:
        if self._passage is not None:
            self._passage.ended = True
        await self._handed_over()

    async def finish(self) -> None:
        await self.end_part()

    async def stop(self) -> None:
        pass  # nothing is held

    async def _handed_over(self) -> None:
        """Wait until the reader has taken everything said so far."""
        if self._taken is not None:
            await self._taken


class WrittenConversation:
    """One written conversation with a gem: send a message, read the reply as it is written."""

    def __init__(
        self,
        *,
        model: Model,
        tools: Toolbox,
        instructions: str,
        late: LateResults,
        history: Sequence[Item] = (),
    ) -> None:
        # `model` has to be told of exactly the tools in `tools`, and `instructions` are
        # the whole instructions, with what the model needs already in them. `late` holds
        # the calls of this conversation that outlive their replies, those of earlier
        # replies included.
        self._model = model
        self._tools = tools
        self._instructions = instructions
        self._late = late
        self._history: list[Item] = list(history)
        # Everything added to the history since the conversation was made, in order.
        self.added: list[Added] = []
        # The tool calls left running at a cut, in order. Each has the placeholder as its
        # result in the history, and is held by `late` until its result is told.
        self.left_running: list[ToolCall] = []
        # The user's message whose reply is being written, until the reply is settled.
        self._open: Item | None = None
        self._open_at = datetime.now(UTC)
        self._writing = False
        self._cut = False
        self._out: asyncio.Queue[_Handed] | None = None
        # Settling the latest reply into the history. Kept so the task is not lost if
        # the reader that started it is cancelled while it runs.
        self._settling: asyncio.Task[WrittenEvent] | None = None

    async def send(
        self, text: str | None, *, at: datetime | None = None
    ) -> AsyncGenerator[WrittenEvent]:
        """Send a message and yield its reply as it is written, ending with `done` or `cut`.

        With `text` None, no message is sent: the reply is written from the history as it
        stands, as when a late search result is to be told.

        `at` is when the message was sent, if not now. Nothing happens until the stream
        is read. One reply at a time: reading a second stream while a reply is being
        written raises `RuntimeError`. A reply that fails raises its failure, after the
        text that was delivered, once it is settled.

        To stop a reply part-way, call `cut`, and the stream ends with `cut`. Closing
        the stream (as with `contextlib.aclosing`) or cancelling its reader stops it the
        same way, without the event. Only then is the reply settled into the history: a
        stream left open holds it. A reply is settled before `done` or `cut` is yielded,
        so the next message can be sent as soon as either is seen.
        """
        if self._writing:
            raise RuntimeError("a reply is still being written")
        self._writing = True
        self._open = None if text is None else {"role": "user", "content": text}
        self._open_at = at or datetime.now(UTC)
        out: asyncio.Queue[_Handed] = asyncio.Queue()
        self._out = out
        reply = Reply(_Page(out))
        # A reply cut before it began is not begun at all: the model is not asked.
        producing = None if self._cut else asyncio.create_task(self._produce(reply, out))
        try:
            while not self._cut and (handed := await out.get()) is not None:
                event, passage, taken = handed
                if passage is not None:
                    passage.handed += len(event.text)
                if taken is not None:
                    taken.set_result(None)
                yield event
            if producing is not None and not self._cut:
                await producing
        finally:
            # Settled in a task of its own, and only waited for here. A reader that is
            # cancelled again while it waits then cannot leave the reply out of the
            # history, or its tool calls without a result. The next message is accepted
            # once that task is done, not before.
            self._settling = asyncio.create_task(self._settle(reply, producing))
            ended = await asyncio.shield(self._settling)
        yield ended

    def cut(self) -> None:
        """Cut the reply being written off where the reader is, as a new message does.

        Its stream ends with `cut`. Called before `send`, the next reply is cut before
        it begins: nothing is kept of it, and the model is not asked for it.
        """
        self._cut = True
        if self._out is not None:
            self._out.put_nowait(None)  # wakes the reader, if it is waiting for text

    async def _produce(self, reply: Reply, out: asyncio.Queue[_Handed]) -> None:
        try:
            await run_reply(
                reply,
                self._model,
                self._tools,
                instructions=self._instructions,
                next_round=self._next_round,
                # The page hands each piece of text to the reader itself.
                said=lambda text: None,
                show=lambda text: out.put_nowait((WrittenEvent("tool.call", text), None, None)),
            )
        finally:
            out.put_nowait(None)

    def _next_round(self) -> RoundInput:
        # As in a call: the least thinking the model allows, except while a tool call has
        # no result in the history yet. Whether writing wants more is not settled.
        return RoundInput(
            [*self._history, *([self._open] if self._open else [])], think=self._late.pending
        )

    async def _settle(self, reply: Reply, producing: asyncio.Task[None] | None) -> WrittenEvent:
        """Stop the reply, then move it into the history as delivered.

        A tool call still running is left running, with the placeholder as its result.
        Return the event that ends the reply's stream, if it is not a failure.
        """
        try:
            if producing is not None:
                producing.cancel()
                await asyncio.gather(producing, return_exceptions=True)
            for call in reply.calls():
                if self._late.settle(call):
                    self.left_running.append(call)
            delivered = reply.delivered(0.0)
            items = delivered.items
            whole = reply.finished and delivered.kept is None
            now = datetime.now(UTC)
            calls = {call.call_id: call for call in reply.calls()}
            if self._open is not None:
                self._add(Added(self._open, self._open_at))
            for item in items:
                if "type" in item and item["type"] == "function_call":
                    self._add(Added(item, now, shown=shown(calls[item["call_id"]])))
                else:
                    self._add(Added(item, now))
            kept = " ".join(item["content"] for item in items if "role" in item)
            if whole:
                return WrittenEvent("done", kept)
            # The history keeps only what was written, as a call keeps only what was
            # heard: a reply cut before any of it was sent leaves nothing there, and
            # one cut while a tool ran keeps the text before it whole. The event still
            # says something was cut.
            return WrittenEvent("cut", kept or CUT_MARK)
        finally:
            self._open = None
            self._out = None
            self._cut = False
            self._writing = False

    def _add(self, added: Added) -> None:
        self._history.append(added.item)
        self.added.append(added)


def shown(call: ToolCall) -> str | None:
    """The text of the `tool.call` event a call sent, worked out again from its arguments."""
    if call.tool is None:
        return None  # a tool the conversation does not have shows nothing
    try:
        return call.tool.shown(call.arguments)
    except Exception:
        return None  # arguments that cannot be read send no event
