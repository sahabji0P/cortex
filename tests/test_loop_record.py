"""What the loop hands to whoever writes down a reply's requests and tool calls."""

import asyncio
from collections.abc import AsyncGenerator, Sequence
from typing import Any

import pytest

from app.entities.loop import Reply, RoundInput, ToolCall, run_reply
from app.entities.model import (
    Item,
    ModelCall,
    ModelEvent,
    RoundDone,
    TextDelta,
    ToolSpec,
    Usage,
)
from app.entities.tools import Tool, Toolbox

_USED = Usage(input=10, cache_read=90, cache_write=0, output=4)


class _Model:
    """A model, scripted: each round plays the next list of events, and is written down."""

    own_instructions = ""

    def __init__(self, *rounds: list[Any]) -> None:
        self._rounds = list(rounds)
        self.requests: list[tuple[list[Item], str, bool]] = []

    async def round(
        self, items: list[Item], *, instructions: str, think: bool
    ) -> AsyncGenerator[ModelEvent]:
        self.requests.append((list(items), instructions, think))
        for event in self._rounds.pop(0):
            if isinstance(event, asyncio.Event):
                await event.wait()
            elif isinstance(event, Exception):
                raise event
            else:
                yield event


class _Part:
    def __init__(self) -> None:
        self.text = ""

    def delivered(self, at: float) -> tuple[str, bool]:
        return self.text, True


class _Surface:
    """A surface that takes everything said at once."""

    def __init__(self) -> None:
        self._part: _Part | None = None

    async def say(self, text: str) -> _Part:
        if self._part is None:
            self._part = _Part()
        self._part.text += text
        return self._part

    async def end_part(self) -> None:
        self._part = None

    async def finish(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class _Request:
    def __init__(self, record: "_Record") -> None:
        self._record = record

    def first(self) -> None:
        self._record.seen.append(("first",))

    def done(self, text: str, calls: Sequence[ModelCall], usage: Usage | None) -> None:
        self._record.seen.append(("done", text, [call.name for call in calls], usage))

    def stopped(self, text: str, error: BaseException) -> None:
        self._record.seen.append(("stopped", text, type(error).__name__))


class _Record:
    """Writes down what it is told, in order."""

    def __init__(self) -> None:
        self.seen: list[tuple[Any, ...]] = []
        self.calls: list[ToolCall] = []

    def sent(self, items: Sequence[Item], think: bool) -> _Request:
        self.seen.append(("sent", list(items), think))
        return _Request(self)

    def tool(self, call: ToolCall) -> None:
        self.seen.append(("tool", call.name))
        self.calls.append(call)


def _tool(name: str, result: str | Exception) -> Tool:
    async def run(arguments: str) -> str:
        if isinstance(result, Exception):
            raise result
        return result

    return Tool(ToolSpec(name, "A tool.", {"type": "object", "properties": {}}), run)


_HISTORY: list[Item] = [{"role": "user", "content": "weather?"}]


async def _reply(model: _Model, tools: Toolbox, record: _Record | None) -> Reply:
    reply = Reply(_Surface())
    await run_reply(
        reply,
        model,
        tools,
        instructions="Be brief.\n",
        next_round=lambda: RoundInput(_HISTORY, think=False),
        said=lambda text: None,
        show=lambda text: None,
        record=record,
    )
    return reply


def _searching() -> _Model:
    return _Model(
        [
            TextDelta("Let me "),
            TextDelta("look."),
            RoundDone([ModelCall("c1", "look", "{}")], _USED),
        ],
        [TextDelta("Sunny."), RoundDone([], _USED)],
    )


async def test_each_request_is_written_down_with_what_it_was_sent_and_how_it_ended() -> None:
    record = _Record()

    await _reply(_searching(), Toolbox([_tool("look", "Sunny, 24.")]), record)

    call_and_result = [
        {"role": "assistant", "content": "Let me look."},
        {"type": "function_call", "call_id": "c1", "name": "look", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "Sunny, 24."},
    ]
    assert record.seen == [
        ("sent", _HISTORY, False),
        ("first",),
        ("first",),
        ("done", "Let me look.", ["look"], _USED),
        ("tool", "look"),
        ("sent", [*_HISTORY, *call_and_result], False),
        ("first",),
        ("done", "Sunny.", [], _USED),
    ]


async def test_writing_a_reply_down_changes_nothing_the_model_is_sent() -> None:
    plain, written = _searching(), _searching()
    tools = Toolbox([_tool("look", "Sunny, 24.")])

    await _reply(plain, tools, None)
    await _reply(written, tools, _Record())

    assert written.requests == plain.requests


async def test_a_request_that_fails_is_written_down_as_stopped_with_its_text() -> None:
    record = _Record()
    model = _Model([TextDelta("Let me"), RuntimeError("the model call failed")])

    with pytest.raises(RuntimeError):
        await _reply(model, Toolbox(), record)

    assert record.seen[-1] == ("stopped", "Let me", "RuntimeError")


async def test_a_request_cut_while_the_model_produces_is_written_down_as_stopped() -> None:
    record = _Record()
    never = asyncio.Event()
    model = _Model([TextDelta("Once upon"), never, RoundDone([], _USED)])

    replying = asyncio.create_task(_reply(model, Toolbox(), record))
    await asyncio.sleep(0.01)
    replying.cancel()
    await asyncio.gather(replying, return_exceptions=True)

    assert record.seen[-1] == ("stopped", "Once upon", "CancelledError")


async def test_a_tool_call_says_whether_its_tool_failed_without_changing_its_result() -> None:
    record = _Record()
    model = _Model(
        [RoundDone([ModelCall("c1", "look", "{}"), ModelCall("c2", "gone", "{}")], None)],
        [RoundDone([ModelCall("c3", "fine", "{}")], None)],
        [RoundDone([], None)],
    )
    tools = Toolbox([_tool("look", ConnectionError("no network")), _tool("fine", "ok")])

    await _reply(model, tools, record)

    assert [(call.name, call.output, call.failed) for call in record.calls] == [
        ("look", "look failed: no network", True),
        ("gone", "Unknown tool: gone", True),
        ("fine", "ok", False),
    ]
