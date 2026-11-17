"""
Written entity: a conversation with a gem in writing, the thinnest one that works.

One message in, one reply out. The reply is made by the same loop as a call's (see
`loop.py`) and delivered as text: each piece reaches the reader as an event of the
stream `send` returns. Nothing routes to it yet, and nothing is stored: the history
lives as long as the object.

The history follows the same rules as a call's. In writing, what was delivered is what
the reader was handed, and the reply runs at most one piece of text ahead of them. A
reader who stops part-way (closes the stream, or is cancelled) leaves the history with
the text they were handed, ending in "…".
"""

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Literal

from app.entities.loop import Reply, RoundInput, ToolCall, run_reply
from app.entities.model import LLM, Item, ModelName, model_for
from app.entities.tools import Toolbox

__all__ = ["WrittenConversation", "WrittenEvent"]

# The result of a tool call that was still running when its reply was cut off.
_STOPPED = "Stopped: the reply that made this call was cut off before the call finished."


@dataclass(frozen=True, slots=True)
class WrittenEvent:
    """One thing that happens in a written reply.

    - `text`       the next piece of the reply's text
    - `tool.call`  a tool call is being made (what it does in `text`, such as a query)
    - `done`       the reply is complete
    """

    type: Literal["text", "tool.call", "done"]
    text: str = ""


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

    def __init__(self, *, llm: LLM, model: ModelName, tools: Toolbox, instructions: str) -> None:
        # `llm` has to be the adapter of the provider that `model` belongs to.
        self._model = model_for(llm, model, tools.specs)
        self._tools = tools
        self._instructions = instructions + self._model.own_instructions
        self._history: list[Item] = []
        self._writing = False

    async def send(self, text: str) -> AsyncGenerator[WrittenEvent]:
        """Send a message and yield its reply as it is written, ending with `done`.

        Nothing happens until the stream is read. One reply at a time: reading a second
        stream while a reply is being written raises `RuntimeError`. A reply that fails
        raises its failure, after the text that was delivered.

        To stop a reply part-way, close the stream (as with `contextlib.aclosing`). Only
        then is the reply settled into the history: a stream left open holds it.
        """
        if self._writing:
            raise RuntimeError("a reply is still being written")
        self._writing = True
        try:
            self._history.append({"role": "user", "content": text})
            out: asyncio.Queue[_Handed] = asyncio.Queue()
            reply = Reply(_Page(out))
            producing = asyncio.create_task(self._produce(reply, out))
            try:
                while (handed := await out.get()) is not None:
                    event, passage, taken = handed
                    if passage is not None:
                        passage.handed += len(event.text)
                    if taken is not None:
                        taken.set_result(None)
                    yield event
                await producing
                yield WrittenEvent("done")
            finally:
                producing.cancel()
                await asyncio.gather(producing, return_exceptions=True)
                await self._settle(reply)
        finally:
            self._writing = False

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
        # The least thinking the model allows, as in a call. Whether writing wants more
        # is not settled.
        return RoundInput(self._history, think=False)

    async def _settle(self, reply: Reply) -> None:
        """Move the reply into the history as it was delivered."""
        stopped = [task for call in reply.calls() if (task := _stop(call)) is not None]
        await asyncio.gather(*stopped, return_exceptions=True)
        self._history.extend(reply.delivered(0.0).items)


def _stop(call: ToolCall) -> asyncio.Task[str] | None:
    """Give a call of a cut reply its result. Return its task if it had to be stopped.

    Nothing would deliver a later result, and holding the next message back for a call
    the reader has left is worse than not having its result, so it is stopped.
    """
    running = call.running
    if call.output is not None or running is None:
        return None
    if running.done() and not running.cancelled():
        call.output = running.result()  # it finished in the instant before the cut
        return None
    call.output = _STOPPED
    running.cancel()
    return running
