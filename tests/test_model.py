"""The model behind a conversation: choosing a provider, and the Anthropic side.

The OpenAI side is covered by `test_voice_conversation`, which runs every scenario
through it.
"""

import asyncio
import contextlib
import json
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from anthropic import AsyncAnthropic, omit
from openai import AsyncOpenAI

from app.adapters.anthropic_adp import AnthropicAdapterClass
from app.adapters.openai_adp import OpenAIAdapterClass
from app.entities import voice_ent
from app.entities.model import (
    CallStarted,
    Item,
    ModelCall,
    RoundDone,
    TextDelta,
    ToolSpec,
    model_for,
    to_messages,
)
from app.entities.voice_ent import VoiceConversation, VoiceEntityClass
from tests.test_voice_conversation import (
    _DELAY,
    _GEM,
    _STT,
    _TTS,
    _Clock,
    _read_until,
    _Search,
)

_SEARCH = ToolSpec(
    name="web_search",
    description="Search the web.",
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    },
)


def _text(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _call_starts(name: str = "web_search") -> SimpleNamespace:
    block = SimpleNamespace(type="tool_use", name=name)
    return SimpleNamespace(type="content_block_start", content_block=block)


def _final(*calls: tuple[str, str], stop_reason: str | None = None) -> SimpleNamespace:
    """The finished message: one `tool_use` block per (id, query) given."""
    content = [
        SimpleNamespace(type="tool_use", id=call_id, name="web_search", input={"query": query})
        for call_id, query in calls
    ]
    return SimpleNamespace(
        content=content, stop_reason=stop_reason or ("tool_use" if calls else "end_turn")
    )


class _Stream:
    def __init__(self, script: list[SimpleNamespace]) -> None:
        self._script = script
        self.closed = False

    async def __aenter__(self) -> "_Stream":
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True

    async def __aiter__(self):
        for step in self._script[:-1]:
            if isinstance(step, asyncio.Event):
                await step.wait()
            else:
                yield step

    async def get_final_message(self) -> SimpleNamespace:
        return self._script[-1]


class _Claude:
    """The Anthropic adapter: each call plays the next scripted stream, ending in its message."""

    def __init__(self, *rounds: list) -> None:
        self._rounds = list(rounds)
        self.requests: list[dict] = []
        self.streams: list[_Stream] = []

    def stream_message(self, **request) -> _Stream:
        self.requests.append(request)
        self.streams.append(_Stream(self._rounds.pop(0)))
        return self.streams[-1]


def _conversation(llm: _Claude, model: str = "Haiku 5.5", search: _Search | None = None):
    stt, tts, clock = _STT(), _TTS(), _Clock()
    conversation = VoiceConversation(
        stt=stt, tts=tts, llm=llm, search=search or _Search(), gem=_GEM, model=model, clock=clock
    )
    return conversation, stt, clock


# ---- translating the history


def test_the_history_becomes_alternating_messages_of_blocks() -> None:
    history: list[Item] = [
        {"role": "user", "content": "Who won the race?"},
        {"role": "assistant", "content": "Let me check."},
        {"type": "function_call", "call_id": "c1", "name": "web_search", "arguments": '{"q": 1}'},
        {"type": "function_call_output", "call_id": "c1", "output": "Norris won."},
        {"role": "assistant", "content": "Norris…"},
        {"role": "developer", "content": "You were interrupted there."},
        {"role": "user", "content": "And second?"},
    ]

    assert to_messages(history) == [
        {"role": "user", "content": [{"type": "text", "text": "Who won the race?"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me check."},
                {"type": "tool_use", "id": "c1", "name": "web_search", "input": {"q": 1}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "Norris won."}],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "Norris…"}]},
        {
            "role": "user",
            "content": [
                # Our own note travels on the user side, marked as not the user's words.
                {"type": "text", "text": "<note>You were interrupted there.</note>"},
                {"type": "text", "text": "And second?"},
            ],
        },
    ]


def test_each_call_of_a_round_is_followed_directly_by_its_own_result() -> None:
    history: list[Item] = [
        {"role": "user", "content": "Compare them."},
        {"type": "function_call", "call_id": "c1", "name": "web_search", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "one"},
        {"type": "function_call", "call_id": "c2", "name": "web_search", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c2", "output": ""},
    ]

    roles = [message["role"] for message in to_messages(history)]
    assert roles == ["user", "assistant", "user", "assistant", "user"]
    last = cast("list[dict]", to_messages(history)[-1]["content"])
    # An empty result is not accepted, so it is given words.
    assert last == [{"type": "tool_result", "tool_use_id": "c2", "content": "No output."}]


def test_a_longer_history_starts_with_the_whole_of_the_shorter_one() -> None:
    history: list[Item] = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi."},
        {"role": "developer", "content": "A note."},
    ]
    before = to_messages(history)
    after = to_messages([*history, {"role": "user", "content": "More"}])

    assert after[:-1] == before[:-1]
    grown = cast("list[dict]", after[-1]["content"])
    assert grown[:-1] == before[-1]["content"]


# ---- one model call


async def test_a_round_is_sent_as_a_messages_request_and_read_back_as_events() -> None:
    llm = _Claude([_text("Let me "), _text("check."), _call_starts(), _final(("toolu_1", "f1"))])
    model = model_for(cast("AnthropicAdapterClass", llm), "Sonnet 5.5", [_SEARCH])

    events = [
        event
        async for event in model.round(
            [{"role": "user", "content": "Who won?"}],
            instructions="Be brief.\n",
            think=True,
        )
    ]

    assert events == [
        TextDelta("Let me "),
        TextDelta("check."),
        CallStarted("web_search"),
        RoundDone([ModelCall("toolu_1", "web_search", json.dumps({"query": "f1"}))]),
    ]
    (request,) = llm.requests
    assert request["model"] == "Sonnet 5.5"
    assert request["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "Who won?"}]}
    ]
    # The instructions go out as given. The line about notes is the caller's to add.
    assert request["system"] == "Be brief.\n"
    assert "<note>" in model.own_instructions
    # The thinking setting is fixed for the model: asking to think does not change it.
    assert request["thinking"] == {"type": "between_tools"}
    assert request["tool_choice"] == {"type": "auto"}
    assert request["cache_control"] == {"type": "ephemeral"}
    (tool,) = request["tools"]
    assert tool["name"] == "web_search"
    assert tool["strict"] is True
    assert tool["input_schema"] == _SEARCH.parameters
    assert llm.streams[0].closed


@pytest.mark.parametrize(
    ("stop_reason", "message"),
    [
        ("refusal", "declined to answer"),
        ("max_tokens", "stopped before finishing"),
        ("model_context_window_exceeded", "stopped before finishing"),
        ("pause_turn", "stopped before finishing"),
        # A stream that broke off between events: no reason was ever given.
        (None, "stopped before finishing"),
    ],
)
async def test_a_round_that_is_refused_or_cut_off_raises(
    stop_reason: str | None, message: str
) -> None:
    final = _final(("toolu_1", "f1"))
    final.stop_reason = stop_reason
    llm = _Claude([_text("I"), final])
    model = model_for(cast("AnthropicAdapterClass", llm), "Haiku 5.5", [_SEARCH])

    with pytest.raises(RuntimeError, match=message):
        async for _ in model.round(
            [{"role": "user", "content": "hi"}], instructions="", think=False
        ):
            pass


@pytest.mark.parametrize(
    ("name", "thinking", "output_config"),
    [
        ("Haiku 5.5", {"type": "disabled"}, omit),
        ("Sonnet 5.5", {"type": "between_tools"}, omit),
        # Opus 5.5 cannot have its thinking turned off: it is asked for as little as it can do.
        ("Opus 5.5", omit, {"effort": "low"}),
    ],
)
async def test_each_anthropic_model_is_asked_to_think_as_little_as_it_can(
    name: str, thinking: object, output_config: object
) -> None:
    llm = _Claude([_text("Paris."), _final()])
    model = model_for(cast("AnthropicAdapterClass", llm), name, [_SEARCH])

    async for _ in model.round([{"role": "user", "content": "hi"}], instructions="", think=True):
        pass

    (request,) = llm.requests
    assert request["model"] == name
    assert request["thinking"] == thinking
    assert request["output_config"] == output_config


class _OpenAI:
    """The OpenAI adapter: every call says one word and is written down."""

    def __init__(self) -> None:
        self.requests: list[dict] = []

    async def stream_response(self, **request):
        self.requests.append(request)

        async def stream():
            yield SimpleNamespace(type="response.output_text.delta", delta="Hi.")
            yield SimpleNamespace(type="response.completed", response=SimpleNamespace(output=[]))

        return stream()


@pytest.mark.parametrize(
    ("name", "normally", "asked_to_think"),
    [
        ("6 Luna", "none", "low"),
        ("6.1 Terra", "none", "low"),
        # 6.1 Sol cannot run without thinking.
        ("6.1 Sol", "low", "low"),
    ],
)
async def test_each_openai_model_is_called_by_name_and_thinks_only_as_much_as_it_must(
    name: str, normally: str, asked_to_think: str
) -> None:
    llm = _OpenAI()
    model = model_for(cast("OpenAIAdapterClass", llm), name, [_SEARCH])

    for think in (False, True):
        async for _ in model.round(
            [{"role": "user", "content": "hi"}], instructions="", think=think
        ):
            pass

    assert [request["model"] for request in llm.requests] == [name, name]
    assert [request["reasoning"]["effort"] for request in llm.requests] == [
        normally,
        asked_to_think,
    ]


# ---- choosing the provider


def test_a_model_needs_the_adapter_of_its_own_provider() -> None:
    with pytest.raises(TypeError, match="needs the Anthropic adapter"):
        model_for(OpenAIAdapterClass(), "Haiku 5.5", [])
    with pytest.raises(TypeError, match="needs the OpenAI adapter"):
        model_for(AnthropicAdapterClass(), "6 Luna", [])


@pytest.mark.parametrize(
    ("named", "configured", "used", "provider"),
    [
        (None, "6 Luna", "6 Luna", "openai"),
        (None, "Sonnet 5.5", "Sonnet 5.5", "anthropic"),
        ("Haiku 5.5", "6 Luna", "Haiku 5.5", "anthropic"),
        ("6 Luna", "Haiku 5.5", "6 Luna", "openai"),
        ("6.1 Terra", "Haiku 5.5", "6.1 Terra", "openai"),
        ("6.1 Sol", "6 Luna", "6.1 Sol", "openai"),
        ("Opus 5.5", "6 Luna", "Opus 5.5", "anthropic"),
    ],
)
async def test_a_conversation_is_opened_on_the_gem_s_model_or_the_configured_one(
    monkeypatch: pytest.MonkeyPatch,
    named: str | None,
    configured: str,
    used: str,
    provider: str,
) -> None:
    built: list[tuple[object, str]] = []
    real = voice_ent.model_for

    def spy(llm, name, tools):
        built.append((llm, name))
        return real(llm, name, tools)

    monkeypatch.setattr(voice_ent, "model_for", spy)
    monkeypatch.setattr(voice_ent.settings, "voice_model", configured)
    adapters = _adapters(openai_key=True, anthropic_key=True)
    entity = _entity(adapters)

    conversation = (await entity.connect(_GEM.model_copy(update={"model": named}))).open()
    await conversation.close()

    assert built == [(adapters[provider], used)]


def _adapters(*, openai_key: bool, anthropic_key: bool) -> dict[str, object]:
    """Both adapters, each with a client (as if its key were set) or without one."""
    openai, anthropic = OpenAIAdapterClass(), AnthropicAdapterClass()
    if openai_key:
        openai._client = AsyncOpenAI(api_key="test")
    if anthropic_key:
        anthropic._client = AsyncAnthropic(api_key="test")
    return {"openai": openai, "anthropic": anthropic}


def _entity(adapters: dict[str, object]) -> VoiceEntityClass:
    stt = _STT()
    return VoiceEntityClass(
        stt=SimpleNamespace(connect_auto_turns=lambda: _connected(stt)),
        tts=_TTS(),
        openai=adapters["openai"],
        anthropic=adapters["anthropic"],
        search=_Search(),
    )


async def _connected(stt: _STT) -> _STT:
    return stt


async def test_a_model_whose_provider_has_no_key_is_refused_when_the_conversation_opens() -> None:
    entity = _entity(_adapters(openai_key=True, anthropic_key=False))

    with pytest.raises(LookupError, match="no Anthropic key is set"):
        await entity.connect(_GEM.model_copy(update={"model": "Opus 5.5"}))

    conversation = (await entity.connect(_GEM.model_copy(update={"model": "6.1 Terra"}))).open()
    await conversation.close()


def test_the_models_are_listed_in_order_with_whether_each_can_be_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(voice_ent.settings, "voice_model", "6 Luna")
    entity = _entity(_adapters(openai_key=True, anthropic_key=False))

    listed = [
        (info.provider, info.name, available, default)
        for info, available, default in entity.models()
        if info.listed
    ]

    assert listed == [
        ("OpenAI", "6.1 Terra", True, False),
        ("OpenAI", "6 Luna", True, True),
        ("OpenAI", "6.1 Sol", True, False),
        ("Anthropic", "Opus 5.5", False, False),
        ("Anthropic", "Sonnet 5.5", False, False),
        ("Anthropic", "Haiku 5.5", False, False),
    ]


def test_a_history_that_opens_with_a_note_still_starts_with_the_user_side() -> None:
    # A conversation in which the assistant speaks first opens with a note of ours.
    messages = to_messages(
        [
            {"role": "developer", "content": "Greet the user."},
            {"role": "assistant", "content": "Hi, how can I help?"},
            {"role": "user", "content": "capital of France?"},
        ]
    )

    assert [message["role"] for message in messages] == ["user", "assistant", "user"]
    assert messages[0]["content"] == [{"type": "text", "text": "<note>Greet the user.</note>"}]


# ---- the OpenAI side, through the real SDK


class _EndlessBody(httpx.AsyncByteStream):
    """A streamed reply that says one word and then never ends."""

    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self):
        yield (
            b"event: response.output_text.delta\n"
            b'data: {"type":"response.output_text.delta","delta":"Hi ","item_id":"x",'
            b'"output_index":0,"content_index":0,"sequence_number":1,"logprobs":[]}\n\n'
        )
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


async def test_cutting_off_an_openai_reply_closes_its_connection_there_and_then() -> None:
    body = _EndlessBody()

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)

    adapter = OpenAIAdapterClass()
    adapter._client = AsyncOpenAI(
        api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(answer))
    )
    model = model_for(adapter, "6 Luna", [])
    speaking = asyncio.Event()

    async def reply() -> None:
        events = model.round([{"role": "user", "content": "hi"}], instructions="", think=False)
        async with contextlib.aclosing(events):
            async for _ in events:
                speaking.set()
                await asyncio.sleep(60)  # busy saying the first words when the user cuts in

    replying = asyncio.create_task(reply())
    async with asyncio.timeout(2):
        await speaking.wait()
    assert not body.closed
    replying.cancel()
    await asyncio.gather(replying, return_exceptions=True)

    # Not left for the garbage collector: until it closes, the model is still writing.
    assert body.closed


# ---- a whole conversation on an Anthropic model


# How a call's opening line reaches an Anthropic model.
_CALL_STARTED = {
    "type": "text",
    "text": "<note>The user has called you. From here on the conversation is spoken.</note>",
}


async def test_a_search_runs_between_two_model_calls_on_an_anthropic_model() -> None:
    llm = _Claude(
        [_call_starts(), _final(("toolu_1", "who won the race"))],
        [_text("Norris won."), _final()],
    )
    conversation, stt, _ = _conversation(llm)

    stt.say("who won the race?")
    outputs = await _read_until(conversation, "assistant.done")
    await conversation.close()

    assert outputs[-1].text == "Norris won."
    first, second = llm.requests
    # The line that opens a call's history is a note, on the user's side with their words.
    assert first["messages"] == [
        {"role": "user", "content": [_CALL_STARTED, {"type": "text", "text": "who won the race?"}]}
    ]
    _, assistant, result = second["messages"]
    assert assistant == {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "web_search",
                "input": {"query": "who won the race"},
            },
        ],
    }
    assert result["role"] == "user"
    (block,) = result["content"]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "toolu_1"
    assert "Norris won in Las Vegas." in block["content"]
    assert second["system"] == first["system"]
    assert second["tools"] == first["tools"]


async def test_interrupting_an_anthropic_model_closes_its_stream_and_keeps_what_was_heard() -> None:
    never = asyncio.Event()
    llm = _Claude([_text("Once upon a time"), never, _final()], [_text("Sure."), _final()])
    conversation, stt, clock = _conversation(llm)

    stt.say("tell me a story")
    await _read_until(conversation, "assistant.delta")
    await asyncio.sleep(0.01)
    clock.advance(_DELAY + 2.5)  # each word takes a second: two of the four were heard
    stt.say("stop")
    await _read_until(conversation, "assistant.done")
    await conversation.close()

    assert llm.streams[0].closed
    assert llm.requests[1]["messages"] == [
        {"role": "user", "content": [_CALL_STARTED, {"type": "text", "text": "tell me a story"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Once upon…"}]},
        {"role": "user", "content": [{"type": "text", "text": "stop"}]},
    ]
