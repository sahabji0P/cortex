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

from app.entities.late_results import LateResults
from app.entities.loop import ToolCall
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
    llm: _LLM,
    look_up: _LookUp | None = None,
    history: list[Item] | None = None,
) -> WrittenConversation:
    return _with_tools(llm, (look_up or _LookUp()).tools(), history)


def _with_tools(
    llm: _LLM, tools: Toolbox, history: list[Item] | None = None
) -> WrittenConversation:
    model = model_for(llm, "6 Luna", tools.specs)
    return WrittenConversation(
        model=model,
        tools=tools,
        instructions="Be brief.",
        late=LateResults(lambda: None),
        history=history or [],
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


_PLACEHOLDER_OF_CALL_1 = (
    "Still running in the background, under call_id call_1. It has not failed. Only if "
    "the user says they no longer want this, stop it with cancel_tool_call and that "
    "call_id. Otherwise leave it alone: its result will reach you by itself, as a separate "
    'call with "deferred_result_for": "call_1". Until then, do not make this call '
    "again, in any wording."
)


async def test_a_tool_still_running_at_a_cut_is_left_running_with_the_placeholder() -> None:
    call = _call("Pune weather")
    llm = _LLM([_delta("Let me look."), _starts(call), _completed(call)], _said("Okay."))
    gate = asyncio.Event()
    look_up = _LookUp(gate=gate)
    conversation = _conversation(llm, look_up)

    async with contextlib.aclosing(conversation.send("weather?")) as stream:
        async for event in stream:
            if event.type == "tool.call":
                break

    # The call is not stopped: it is handed back, still running.
    [left] = conversation.left_running
    assert left.call_id == "call_1"
    assert left.running is not None and not left.running.done()
    await _read(conversation, "never mind")
    sent = llm.requests[1]["input"]
    # The text said before the call was complete, so it is kept as it is.
    assert sent[1] == {"role": "assistant", "content": "Let me look."}
    # Its result is the placeholder a call uses. Nothing is made up to mark the cut.
    assert sent[3] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": _PLACEHOLDER_OF_CALL_1,
    }
    assert sent[4] == {"role": "user", "content": "never mind"}

    gate.set()
    async with asyncio.timeout(2):
        assert await left.running == "Found it."
    assert look_up.ended == ["finished"]
    # Delivering the result is the caller's job: the history is not touched.
    assert [entry.item for entry in conversation.added][3]["output"] == _PLACEHOLDER_OF_CALL_1
    # What it stands for is handed back with it, for whoever stores it.
    assert conversation.added[3].mark == "placeholder"


async def test_a_tool_that_finished_before_the_cut_is_not_handed_back() -> None:
    call = _call("Pune weather")
    llm = _LLM([_starts(call), _completed(call)], [asyncio.Event()])
    conversation = _conversation(llm)

    async with asyncio.timeout(2), contextlib.aclosing(conversation.send("weather?")) as stream:
        assert (await anext(stream)).type == "tool.call"
        # The tool answers at once; the second round waits for ever.
        await asyncio.sleep(0.02)
        assert len(llm.requests) == 2
        conversation.cut()
        assert [event.type async for event in stream] == ["cut"]

    assert conversation.left_running == []
    assert conversation.added[2].item == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "Found it.",
    }


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


class _SlowToStop(_LLM):
    """A model whose first reply says a word, then takes a while to stop when told to."""

    def __init__(self, *rounds: list) -> None:
        super().__init__(*rounds)
        self.let_go = asyncio.Event()
        self.ended: list[str] = []

    async def stream_response(self, **request):
        if self.requests:
            return await super().stream_response(**request)
        self.requests.append({**request, "input": [dict(item) for item in request["input"]]})

        async def stream():
            yield _delta("Let me look.")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await self.let_go.wait()
                self.ended.append("stopped")
                raise

        return stream()


async def test_a_reader_cancelled_twice_still_leaves_the_reply_in_the_history() -> None:
    llm = _SlowToStop(_said("Sunny."))
    conversation = _conversation(llm)
    began = asyncio.Event()

    async def reader() -> None:
        async for _ in conversation.send("what is the weather?"):
            began.set()

    reading = asyncio.create_task(reader())
    async with asyncio.timeout(2):
        await began.wait()
        # The first cancel stops the reply. The second lands while the reply is being
        # settled, which is still waiting for the model to stop.
        reading.cancel()
        await asyncio.sleep(0.01)
        reading.cancel()
        await asyncio.gather(reading, return_exceptions=True)
        assert reading.cancelled()

        # The reply is not settled yet, so the next message is refused for now.
        with pytest.raises(RuntimeError):
            await _read(conversation, "too soon")

        llm.let_go.set()
        await asyncio.sleep(0.01)
        assert llm.ended == ["stopped"]

        # Settled all the same: what was said, ending in "…".
        await _read(conversation, "never mind")
    sent = llm.requests[1]["input"]
    assert sent == [
        {"role": "user", "content": "what is the weather?"},
        {"role": "assistant", "content": "Let me look.…"},
        {"role": "user", "content": "never mind"},
    ]


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


async def test_a_reply_cut_before_it_begins_asks_nothing_and_keeps_the_message() -> None:
    llm = _LLM()
    conversation = _conversation(llm)

    conversation.cut()
    events = await _read(conversation, "hello")

    assert events == [WrittenEvent("cut", "…")]
    assert llm.requests == []
    # No reply got through, yet the message was sent: it is in the history as it stands.
    assert [entry.item for entry in conversation.added] == [{"role": "user", "content": "hello"}]


async def test_a_message_sent_before_any_reply_got_through_follows_the_one_before() -> None:
    llm = _LLM(_said("Sunny."))
    # The message before it is stored, and no reply to it is.
    conversation = _conversation(llm, history=[{"role": "user", "content": "a story"}])

    await _read(conversation, "no, the weather")

    # Two user items in a row, each as it was sent: the reply answers both.
    assert llm.requests[0]["input"] == [
        {"role": "user", "content": "a story"},
        {"role": "user", "content": "no, the weather"},
    ]
    assert [entry.item for entry in conversation.added] == [
        {"role": "user", "content": "no, the weather"},
        {"role": "assistant", "content": "Sunny."},
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


# ---- a reply with no new message, and thinking


async def test_a_reply_can_be_written_with_no_new_message() -> None:
    llm = _LLM(_said("It is sunny in Pune."))
    earlier: list[Item] = [
        {"role": "user", "content": "weather?"},
        {"type": "function_call", "call_id": "c", "name": "look_up", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c", "output": "Sunny."},
    ]
    conversation = _conversation(llm, history=earlier)

    events = await _read_none(conversation)

    assert events == [
        WrittenEvent("text", "It is sunny in Pune."),
        WrittenEvent("done", "It is sunny in Pune."),
    ]
    # The request is the history as it stands: no message is added to it.
    assert llm.requests[0]["input"] == earlier
    assert [entry.item for entry in conversation.added] == [
        {"role": "assistant", "content": "It is sunny in Pune."}
    ]


async def test_a_reply_with_no_message_cut_before_it_begins_adds_nothing() -> None:
    llm = _LLM()
    conversation = _conversation(llm)

    conversation.cut()
    assert await _read_none(conversation) == [WrittenEvent("cut", "…")]
    assert llm.requests == []
    assert conversation.added == []


async def test_the_model_thinks_a_little_while_a_call_of_an_earlier_reply_has_no_result() -> None:
    # A call an earlier reply left running, which the first round's tool call ends.
    late = LateResults(lambda: None)
    earlier = ToolCall(
        "call_0", "look_up", "{}", running=asyncio.create_task(asyncio.Event().wait())
    )
    late.settle(earlier)

    async def ends_it(arguments: str) -> str:
        late.stop()
        return "Found it."

    spec = _LookUp().tools().specs[0]
    tools = Toolbox([Tool(spec, ends_it)])
    call = _call("Pune weather")
    llm = _LLM([_starts(call), _completed(call)], _said("Sunny."))
    conversation = WrittenConversation(
        model=model_for(llm, "6 Luna", tools.specs),
        tools=tools,
        instructions="Be brief.",
        late=late,
    )

    await _read(conversation, "weather?")

    assert [request["reasoning"] for request in llm.requests] == [
        {"effort": "low"},
        {"effort": "none"},
    ]


async def _read_none(conversation: WrittenConversation) -> list[WrittenEvent]:
    async with asyncio.timeout(2):
        return [event async for event in conversation.send(None)]
