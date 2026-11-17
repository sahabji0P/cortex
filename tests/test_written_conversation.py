"""The written conversation with the model and its one tool faked: messages in, text out.

It runs on the same loop as a call, so these tests are about what is particular to
writing: what counts as delivered when the reader stops, and one reply at a time.
"""

import asyncio
import contextlib
import json
from types import SimpleNamespace

import pytest

from app.entities.model import ToolSpec
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


def _conversation(llm: _LLM, look_up: _LookUp | None = None) -> WrittenConversation:
    tools = (look_up or _LookUp()).tools()
    return WrittenConversation(llm=llm, model="6 Luna", tools=tools, instructions="Be brief.")


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
        WrittenEvent("done"),
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
        WrittenEvent("done"),
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
    assert sent[4] == {"role": "user", "content": "never mind"}


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
