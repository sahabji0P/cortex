"""The written conversation with the model and its one tool faked: messages in, text out.

It runs on the same loop as a call, so these tests are about what is particular to
writing: what counts as delivered when the reader stops or the reply is cut, what is
handed back to be stored, and one reply at a time.
"""

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.entities.model import Item, ToolSpec, model_for
from app.entities.tools import Tool, Toolbox
from app.entities.written_ent import WrittenConversation, WrittenEvent


def _delta(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="response.output_text.delta", delta=text)


def _call(query: str, call_id: str = "call_1") -> SimpleNamespace:
    return SimpleNamespace(
        type="function_call",
        call_id=call_id,
        name="look_up",
        arguments=json.dumps({"query": query}),
    )


def _starts(call: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(type="response.output_item.added", item=call)


def _completed(*output: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(type="response.completed", response=SimpleNamespace(output=list(output)))


class _LLM:
    """The model: each call plays the next scripted list of stream events.

    A script entry of `asyncio.Event` pauses the stream there until the test sets it.
    """

    def __init__(self, *rounds: list) -> None:
        self._rounds = list(rounds)
        self.requests: list[dict] = []

    async def stream_response(self, **request):
        sent = [dict(item) for item in request["input"]]
        if self.requests:
            # Each request starts with the whole of the one before it.
            before = self.requests[-1]["input"]
            assert sent[: len(before)] == before, "an earlier item was changed"
        self.requests.append({**request, "input": sent})
        script = self._rounds.pop(0)

        async def stream():
            for step in script:
                if isinstance(step, asyncio.Event):
                    await step.wait()
                else:
                    yield step

        return stream()


class _LookUp:
    """A tool that waits for its gate, if it has one, and records how each call ended."""

    def __init__(self, gate: asyncio.Event | None = None) -> None:
        self.gate = gate
        self.ended: list[str] = []

    async def run(self, arguments: str) -> str:
        try:
            if self.gate is not None:
                await self.gate.wait()
        except asyncio.CancelledError:
            self.ended.append("cancelled")
            raise
        self.ended.append("finished")
        return "Found it."

    def tools(self) -> Toolbox:
        spec = ToolSpec(
            name="look_up",
            description="Look something up.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        )
        return Toolbox([Tool(spec, self.run, shown=lambda a: json.loads(a)["query"])])


def _conversation(
    llm: _LLM, look_up: _LookUp | None = None, history: list[Item] | None = None
) -> WrittenConversation:
    return _with_tools(llm, (look_up or _LookUp()).tools(), history)


def _with_tools(
    llm: _LLM, tools: Toolbox, history: list[Item] | None = None
) -> WrittenConversation:
    model = model_for(llm, "6 Luna", tools.specs)
    return WrittenConversation(
        model=model, tools=tools, instructions="Be brief.", history=history or []
    )


async def _read(conversation: WrittenConversation, text: str) -> list[WrittenEvent]:
    async with asyncio.timeout(2):
        return [event async for event in conversation.send(text)]


def _said(text: str) -> list:
    return [_delta(text), _completed()]


async def test_a_reply_is_streamed_and_joins_the_history_whole() -> None:
    llm = _LLM([_delta("Hi "), _delta("there."), _completed()], _said("Fine."))
    conversation = _conversation(llm)

    events = await _read(conversation, "hello")
    assert events == [
        WrittenEvent("text", "Hi "),
        WrittenEvent("text", "there."),
        WrittenEvent("done", "Hi there."),
    ]
    assert llm.requests[0]["instructions"] == "Be brief."

    await _read(conversation, "how are you?")
    assert llm.requests[1]["input"] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi there."},
        {"role": "user", "content": "how are you?"},
    ]


async def test_a_tool_call_is_shown_and_its_result_goes_back_to_the_model() -> None:
    call = _call("Pune weather")
    llm = _LLM(
        [_delta("Let me look."), _starts(call), _completed(call)],
        _said("Sunny."),
        _said("Bye."),
    )
    conversation = _conversation(llm)

    events = await _read(conversation, "weather?")
    assert events == [
        WrittenEvent("text", "Let me look."),
        WrittenEvent("tool.call", "Pune weather"),
        WrittenEvent("text", "Sunny."),
        WrittenEvent("done", "Let me look. Sunny."),
    ]
    await _read(conversation, "thanks")
    assert llm.requests[2]["input"] == [
        {"role": "user", "content": "weather?"},
        {"role": "assistant", "content": "Let me look."},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "look_up",
            "arguments": call.arguments,
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "Found it."},
        {"role": "assistant", "content": "Sunny."},
        {"role": "user", "content": "thanks"},
    ]


async def test_a_reader_who_stops_leaves_only_the_text_they_were_handed() -> None:
    # The model has the whole reply ready; the reader takes one piece and closes.
    llm = _LLM(
        [_delta("Once upon "), _delta("a time "), _delta("there was a dragon."), _completed()],
        _said("Sure."),
    )
    conversation = _conversation(llm)

    async with contextlib.aclosing(conversation.send("a story")) as stream:
        assert await anext(stream) == WrittenEvent("text", "Once upon ")

    await _read(conversation, "stop")
    assert llm.requests[1]["input"] == [
        {"role": "user", "content": "a story"},
        {"role": "assistant", "content": "Once upon…"},
        {"role": "user", "content": "stop"},
    ]


async def test_a_reader_cancelled_mid_reply_is_treated_the_same() -> None:
    never = asyncio.Event()
    llm = _LLM([_delta("Once upon "), _delta("a time "), never], _said("Sure."))
    conversation = _conversation(llm)
    handed: list[WrittenEvent] = []
    two_in = asyncio.Event()

    async def read() -> None:
        async for event in conversation.send("a story"):
            handed.append(event)
            if len(handed) == 2:
                two_in.set()

    reading = asyncio.create_task(read())
    await two_in.wait()
    reading.cancel()
    await asyncio.gather(reading, return_exceptions=True)

    await _read(conversation, "stop")
    assert llm.requests[1]["input"][1] == {"role": "assistant", "content": "Once upon a time…"}


async def test_a_tool_still_running_at_a_cut_is_stopped_and_given_a_result() -> None:
    call = _call("Pune weather")
    llm = _LLM([_delta("Let me look."), _starts(call), _completed(call)], _said("Okay."))
    look_up = _LookUp(gate=asyncio.Event())
    conversation = _conversation(llm, look_up)

    async with contextlib.aclosing(conversation.send("weather?")) as stream:
        async for event in stream:
            if event.type == "tool.call":
                break

    # Nothing is left running, and the call has a result the model can read.
    assert look_up.ended == ["cancelled"]
    await _read(conversation, "never mind")
    sent = llm.requests[1]["input"]
    # The text said before the call was complete, so it is kept as it is.
    assert sent[1] == {"role": "assistant", "content": "Let me look."}
    assert sent[3]["type"] == "function_call_output"
    assert sent[3]["call_id"] == "call_1"
    assert sent[3]["output"].startswith("Stopped")
    # The text before the call was complete, so the cut is marked after the call.
    assert sent[4] == {"role": "assistant", "content": "…"}
    assert sent[5] == {"role": "user", "content": "never mind"}


async def test_a_second_message_while_a_reply_is_written_is_refused() -> None:
    gate = asyncio.Event()
    llm = _LLM([_delta("Thinking "), gate, _delta("done."), _completed()], _said("Yes."))
    conversation = _conversation(llm)

    async with contextlib.aclosing(conversation.send("first")) as first:
        await anext(first)
        with pytest.raises(RuntimeError):
            await anext(conversation.send("second"))
        gate.set()
        assert [event.type async for event in first] == ["text", "done"]

    # Refused means not sent: the history has no trace of it.
    await _read(conversation, "third")
    assert [item.get("content") for item in llm.requests[1]["input"]] == [
        "first",
        "Thinking done.",
        "third",
    ]


async def test_a_failed_reply_raises_and_keeps_what_was_delivered() -> None:
    llm = _LLM([_delta("Half "), SimpleNamespace(type="error", message="boom")], _said("Ok."))
    conversation = _conversation(llm)

    handed: list[WrittenEvent] = []
    with pytest.raises(RuntimeError, match="boom"):
        async for event in conversation.send("go"):
            handed.append(event)
    assert handed == [WrittenEvent("text", "Half ")]

    await _read(conversation, "again")
    assert llm.requests[1]["input"][1] == {"role": "assistant", "content": "Half…"}


async def test_the_next_message_can_be_sent_as_soon_as_done_is_seen() -> None:
    llm = _LLM(_said("Hi."), _said("Again."))
    conversation = _conversation(llm)

    # The reader stops at `done` without reading on, and keeps the stream open.
    stream = conversation.send("hello")
    async with asyncio.timeout(2):
        async for event in stream:
            if event.type == "done":
                break

    # The first reply is already in the history, and the conversation is free.
    await _read(conversation, "and again")
    assert llm.requests[1]["input"] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi."},
        {"role": "user", "content": "and again"},
    ]
    await stream.aclose()


async def test_a_reader_cancelled_twice_still_leaves_the_reply_in_the_history() -> None:
    ended: list[str] = []
    let_go = asyncio.Event()

    async def slow_to_stop(arguments: str) -> str:
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            # A tool that takes a while to stop when it is told to.
            await let_go.wait()
            ended.append("stopped")
            raise
        return "Found it."

    spec = ToolSpec(
        name="look_up",
        description="Look something up.",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    tools = Toolbox([Tool(spec, slow_to_stop, shown=lambda a: json.loads(a)["query"])])
    call = _call("the weather")
    llm = _LLM([_delta("Let me look."), _starts(call), _completed(call)], _said("Sunny."))
    conversation = _with_tools(llm, tools)
    looking = asyncio.Event()

    async def reader() -> None:
        async for event in conversation.send("what is the weather?"):
            if event.type == "tool.call":
                looking.set()

    reading = asyncio.create_task(reader())
    async with asyncio.timeout(2):
        await looking.wait()
        # The first cancel stops the reply. The second lands while the reply is being
        # settled, which is still waiting for the tool to stop.
        reading.cancel()
        await asyncio.sleep(0.01)
        reading.cancel()
        await asyncio.gather(reading, return_exceptions=True)
        assert reading.cancelled()

        # The reply is not settled yet, so the next message is refused for now.
        with pytest.raises(RuntimeError):
            await _read(conversation, "too soon")

        let_go.set()
        await asyncio.sleep(0.01)
        assert ended == ["stopped"]

        # Settled all the same: what was said, and the call with a result of its own.
        await _read(conversation, "never mind")
    sent = llm.requests[1]["input"]
    assert [item.get("role") or item["type"] for item in sent] == [
        "user",
        "assistant",
        "function_call",
        "function_call_output",
        "assistant",
        "user",
    ]
    assert sent[1]["content"] == "Let me look."
    assert sent[4]["content"] == "…"
    assert sent[3]["output"].startswith("Stopped:")


# ---- given a history, handing back what was added, and cutting


async def test_it_starts_from_the_history_it_is_given_and_hands_back_what_it_added() -> None:
    call = _call("Pune weather")
    llm = _LLM([_delta("Let me look."), _starts(call), _completed(call)], _said("Sunny."))
    earlier: list[Item] = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi."},
    ]
    conversation = _conversation(llm, history=earlier)
    sent_at = datetime(2020, 1, 1, 8, 0, tzinfo=UTC)

    await _read_at(conversation, "weather?", sent_at)

    assert llm.requests[0]["input"] == [*earlier, {"role": "user", "content": "weather?"}]
    added = conversation.added
    assert [entry.item for entry in added] == [
        {"role": "user", "content": "weather?"},
        {"role": "assistant", "content": "Let me look."},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "look_up",
            "arguments": call.arguments,
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "Found it."},
        {"role": "assistant", "content": "Sunny."},
    ]
    # Only the call carries what its event showed.
    assert [entry.shown for entry in added] == [None, None, "Pune weather", None, None]
    assert added[0].at == sent_at
    assert all(entry.at > sent_at for entry in added[1:])


async def test_a_cut_ends_the_stream_with_what_is_kept() -> None:
    gate = asyncio.Event()
    llm = _LLM([_delta("Once upon "), gate, _delta("a time."), _completed()])
    conversation = _conversation(llm)

    async with asyncio.timeout(2):
        stream = conversation.send("a story")
        assert await anext(stream) == WrittenEvent("text", "Once upon ")
        conversation.cut()
        assert [event async for event in stream] == [WrittenEvent("cut", "Once upon…")]

    assert [entry.item for entry in conversation.added] == [
        {"role": "user", "content": "a story"},
        {"role": "assistant", "content": "Once upon…"},
    ]


async def test_a_reply_cut_before_it_begins_is_kept_as_the_mark_and_asks_nothing() -> None:
    llm = _LLM()
    conversation = _conversation(llm)

    conversation.cut()
    events = await _read(conversation, "hello")

    assert events == [WrittenEvent("cut", "…")]
    assert llm.requests == []
    assert [entry.item for entry in conversation.added] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "…"},
    ]


async def test_a_failed_reply_is_handed_back_as_delivered() -> None:
    llm = _LLM([_delta("Half "), SimpleNamespace(type="error", message="boom")])
    conversation = _conversation(llm)

    with pytest.raises(RuntimeError, match="boom"):
        await _read(conversation, "go")

    assert [entry.item for entry in conversation.added] == [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "Half…"},
    ]


async def _read_at(conversation: WrittenConversation, text: str, at: datetime) -> None:
    async with asyncio.timeout(2):
        assert [event.type async for event in conversation.send(text, at=at)][-1] == "done"
