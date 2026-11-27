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
    SEARCH_TOOL,
    CallStarted,
    Item,
    Model,
    ModelCall,
    RoundDone,
    Structured,
    TextDelta,
    ToolSpec,
    Usage,
    model_for,
    own_instructions,
    to_messages,
    tools_declared,
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


def _round_of_calls(count: int, name: str = "web_search") -> list[Item]:
    """A question, then a round of `count` calls, each followed by its own result."""
    history: list[Item] = [{"role": "user", "content": "Look them all up."}]
    for number in range(count):
        history.append(
            {"type": "function_call", "call_id": f"c{number}", "name": name, "arguments": "{}"}
        )
        history.append(
            {"type": "function_call_output", "call_id": f"c{number}", "output": f"r{number}"}
        )
    return history


def _marked(request: dict) -> list[int]:
    """Where a request's blocks carry a cache mark of their own, counted from the end."""
    blocks = [block for message in request["messages"] for block in message["content"]]
    return [len(blocks) - 1 - at for at, block in enumerate(blocks) if "cache_control" in block]


async def test_a_long_request_gets_a_second_cache_mark_far_enough_back() -> None:
    # Twelve calls in one round are 24 blocks after the question: further back than the
    # provider looks from the end for what the request before cached.
    llm = _Claude([_final()], [_final()])
    model = model_for(cast("AnthropicAdapterClass", llm), "Haiku 5.5", [_SEARCH])

    for history in (_round_of_calls(12), _round_of_calls(8)):
        async for _ in model.round(history, instructions="", think=False):
            pass

    long, short = llm.requests
    assert _marked(long) == [18]
    assert long["cache_control"] == {"type": "ephemeral"}
    # A request of 18 blocks or fewer has only the mark at its end.
    assert _marked(short) == []
    assert short["cache_control"] == {"type": "ephemeral"}


async def test_the_second_cache_mark_starts_at_nineteen_blocks() -> None:
    llm = _Claude([_final()], [_final()])
    model = model_for(cast("AnthropicAdapterClass", llm), "Haiku 5.5", [_SEARCH])

    # 18 blocks (nine calls, no question), then 19 (the question too).
    for history in (_round_of_calls(9)[1:], _round_of_calls(9)):
        async for _ in model.round(history, instructions="", think=False):
            pass

    eighteen, nineteen = llm.requests
    assert _marked(eighteen) == []
    # The mark goes on the first block.
    assert _marked(nineteen) == [18]


def _reads_cache(request: dict, added: int) -> bool:
    """Whether the request finds what the one before cached at its end, `added` blocks
    back: each mark looks at its own block and the 19 before it."""
    return any(mark <= added <= mark + 19 for mark in [0, *_marked(request)])


async def test_the_two_marks_reach_back_thirty_seven_blocks_without_a_gap() -> None:
    llm = _Claude([_final()])
    model = model_for(cast("AnthropicAdapterClass", llm), "Haiku 5.5", [_SEARCH])

    async for _ in model.round(_round_of_calls(19), instructions="", think=False):
        pass

    (request,) = llm.requests
    assert all(_reads_cache(request, added) for added in range(38))
    assert not _reads_cache(request, 38)


async def test_the_second_cache_mark_passes_over_a_result_that_loads_tools() -> None:
    found = ToolSpec(
        name="tracker__close",
        description="Close an issue.",
        parameters={"type": "object", "properties": {}},
        deferred=True,
    )
    search = ToolSpec(name=SEARCH_TOOL, description="Find tools.", parameters={"type": "object"})
    # The search's result is the block 18 before the end: 9 calls with results follow it.
    history: list[Item] = [
        {"role": "user", "content": "Close it."},
        {"type": "function_call", "call_id": "s1", "name": SEARCH_TOOL, "arguments": "{}"},
        {
            "type": "function_call_output",
            "call_id": "s1",
            "output": json.dumps({"tools": ["tracker__close"]}),
        },
        *_round_of_calls(9, "tracker__close")[1:],
    ]
    llm = _Claude([_final()])
    model = model_for(cast("AnthropicAdapterClass", llm), "Haiku 5.5", [found, search])

    async for _ in model.round(history, instructions="", think=False):
        pass

    (request,) = llm.requests
    # The mark goes on the search's call, the block before its result.
    assert _marked(request) == [19]
    blocks = [block for message in request["messages"] for block in message["content"]]
    assert blocks[-19]["type"] == "tool_result"
    assert isinstance(blocks[-19]["content"], list)
    assert blocks[-20]["type"] == "tool_use"


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


_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"done": {"type": "array", "items": {"type": "string"}}},
    "required": ["done"],
    "additionalProperties": False,
}
_STRUCTURED = Structured(name="summary", schema=_SCHEMA, max_tokens=32_000)

# A history in which a search loaded a deferred tool, and the tool was called.
_DEFERRED = ToolSpec(
    name="weather",
    description="The weather in a city.",
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
    deferred=True,
)
_LOOKUP = ToolSpec(
    name=SEARCH_TOOL,
    description="Find a tool.",
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
)
_SEARCHED: list[Item] = [
    {"role": "user", "content": "weather in Pune?"},
    {"type": "function_call", "call_id": "s1", "name": SEARCH_TOOL, "arguments": "{}"},
    {"type": "function_call_output", "call_id": "s1", "output": '{"tools": ["weather"]}'},
    {"type": "function_call", "call_id": "c1", "name": "weather", "arguments": "{}"},
    {"type": "function_call_output", "call_id": "c1", "output": "31C"},
]


async def test_a_structured_anthropic_round_writes_to_a_schema_with_no_tool_use_or_marks() -> None:
    llm = _Claude([_text('{"done": []}'), _final()])
    model = model_for(cast("AnthropicAdapterClass", llm), "Haiku 5.5", [_SEARCH])

    async for _ in model.round(
        [{"role": "user", "content": "hi"}],
        instructions="Ours.",
        think=False,
        structured=_STRUCTURED,
    ):
        pass

    (request,) = llm.requests
    assert request["system"] == "Ours."
    assert request["max_tokens"] == 32_000
    assert request["output_config"] == {"format": {"type": "json_schema", "schema": _SCHEMA}}
    assert request["tool_choice"] == {"type": "none"}
    # A history with no search loading a tool needs no tools.
    assert request["tools"] == []
    # Nothing is marked for a cache: not the request, not a block far back.
    assert "cache_control" not in request
    assert not any(
        "cache_control" in block for message in request["messages"] for block in message["content"]
    )


async def test_a_structured_round_keeps_opus_thinking_as_little_as_it_can() -> None:
    llm = _Claude([_text('{"done": []}'), _final()])
    model = model_for(cast("AnthropicAdapterClass", llm), "Opus 5.5", [_SEARCH])

    async for _ in model.round(
        [{"role": "user", "content": "hi"}], instructions="", think=False, structured=_STRUCTURED
    ):
        pass

    assert llm.requests[0]["output_config"] == {
        "effort": "low",
        "format": {"type": "json_schema", "schema": _SCHEMA},
    }


async def test_a_structured_anthropic_round_is_told_of_the_tools_when_a_search_loaded_one() -> None:
    llm = _Claude([_text('{"done": []}'), _final()])
    model = model_for(cast("AnthropicAdapterClass", llm), "Haiku 5.5", [_LOOKUP, _DEFERRED])

    async for _ in model.round(_SEARCHED, instructions="", think=False, structured=_STRUCTURED):
        pass

    (request,) = llm.requests
    assert [tool["name"] for tool in request["tools"]] == [SEARCH_TOOL, "weather"]
    assert request["tool_choice"] == {"type": "none"}


def test_which_histories_need_the_tools_told() -> None:
    names = {SEARCH_TOOL, "weather"}
    plain_calls = _SEARCHED[:1] + _SEARCHED[3:]
    found_nothing: list[Item] = [
        *_SEARCHED[:2],
        {"type": "function_call_output", "call_id": "s1", "output": "the search failed"},
    ]

    assert tools_declared("Anthropic", _SEARCHED, names)
    # Calls and results alone are taken with no tools declared (measured on Haiku 5.5
    # and Sonnet 5.5); a search's result that loads a tool is not.
    assert not tools_declared("Anthropic", plain_calls, names)
    assert not tools_declared("Anthropic", found_nothing, names)
    assert not tools_declared("Anthropic", _SEARCHED, {SEARCH_TOOL})
    # Not measured on OpenAI: it is always told of them.
    assert tools_declared("OpenAI", [{"role": "user", "content": "hi"}], names)


async def test_a_structured_openai_round_writes_to_a_strict_schema_with_tool_use_off() -> None:
    openai = _OpenAI()
    model = model_for(cast("OpenAIAdapterClass", openai), "6 Luna", [_SEARCH])

    for structured in (None, _STRUCTURED):
        async for _ in model.round(
            [{"role": "user", "content": "hi"}],
            instructions="",
            think=False,
            structured=structured,
        ):
            pass

    reply, written = openai.requests
    assert (reply["tool_choice"], written["tool_choice"]) == ("auto", "none")
    assert "text" not in reply
    assert written["text"] == {
        "format": {"type": "json_schema", "name": "summary", "schema": _SCHEMA, "strict": True}
    }
    # No ceiling is sent to this provider, and it is told of the same tools.
    assert "max_output_tokens" not in written
    assert written["tools"] == reply["tools"]


async def test_an_openai_refusal_raises() -> None:
    class _Refusing(_OpenAI):
        async def stream_response(self, **request):
            self.requests.append(request)

            async def stream():
                yield SimpleNamespace(type="response.refusal.delta", delta="I can't.")
                yield SimpleNamespace(
                    type="response.completed", response=SimpleNamespace(output=[])
                )

            return stream()

    model = model_for(cast("OpenAIAdapterClass", _Refusing()), "6 Luna", [])

    with pytest.raises(RuntimeError, match="declined"):
        async for _ in model.round(
            [{"role": "user", "content": "hi"}],
            instructions="",
            think=False,
            structured=_STRUCTURED,
        ):
            pass


def test_what_a_model_needs_in_its_instructions_is_known_by_its_name() -> None:
    openai = model_for(cast("OpenAIAdapterClass", _OpenAI()), "6 Luna", [])
    claude = model_for(cast("AnthropicAdapterClass", _Claude()), "Haiku 5.5", [])

    assert own_instructions("6 Luna") == openai.own_instructions == ""
    assert own_instructions("Opus 5.5") == claude.own_instructions != ""


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
    await _read_until(conversation, "assistant.word")
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


# ---- what a round was charged for


async def _one_round(model: Model) -> list[object]:
    return [
        event
        async for event in model.round(
            [{"role": "user", "content": "hello"}], instructions="Be brief.\n", think=False
        )
    ]


async def test_an_anthropic_round_ends_with_what_the_provider_says_it_used() -> None:
    final = _final()
    final.usage = SimpleNamespace(
        input_tokens=12,
        cache_read_input_tokens=3000,
        cache_creation_input_tokens=40,
        output_tokens=7,
    )
    llm = _Claude([_text("Hi."), final])
    model = model_for(cast("AnthropicAdapterClass", llm), "Haiku 5.5", [])

    events = await _one_round(model)

    # The provider's input count already leaves out what a cache held.
    assert events[-1] == RoundDone([], Usage(input=12, cache_read=3000, cache_write=40, output=7))


async def test_an_openai_round_ends_with_what_the_provider_says_it_used() -> None:
    used = SimpleNamespace(
        input_tokens=3400,
        input_tokens_details=SimpleNamespace(cached_tokens=3100, cache_write_tokens=0),
        output_tokens=85,
        output_tokens_details=SimpleNamespace(reasoning_tokens=20),
    )

    class _OpenAI:
        async def stream_response(self, **request: object):
            async def stream():
                yield SimpleNamespace(
                    type="response.completed", response=SimpleNamespace(output=[], usage=used)
                )

            return stream()

    model = model_for(cast("OpenAIAdapterClass", _OpenAI()), "6 Luna", [])

    events = await _one_round(model)

    # The provider's input count includes what a cache held: here it is taken out.
    assert events[-1] == RoundDone(
        [], Usage(input=300, cache_read=3100, cache_write=0, output=85, thinking=20)
    )


@pytest.mark.parametrize(
    "used",
    [
        SimpleNamespace(total_tokens=3),
        SimpleNamespace(input_tokens=None, output_tokens=4),
        SimpleNamespace(input_tokens=12, output_tokens="4"),
    ],
)
async def test_a_reply_is_not_failed_for_a_usage_that_cannot_be_read(used: object) -> None:
    # As an answer by way of a proxy may have it. The count is only written down.
    final = _final()
    final.usage = used
    claude = model_for(
        cast("AnthropicAdapterClass", _Claude([_text("Hi."), final])), "Haiku 5.5", []
    )

    class _OpenAI:
        async def stream_response(self, **request: object):
            async def stream():
                yield SimpleNamespace(
                    type="response.completed", response=SimpleNamespace(output=[], usage=used)
                )

            return stream()

    openai = model_for(cast("OpenAIAdapterClass", _OpenAI()), "6 Luna", [])

    assert (await _one_round(claude))[-1] == RoundDone([], None)
    assert (await _one_round(openai))[-1] == RoundDone([], None)


async def test_a_round_whose_provider_reports_no_use_ends_with_none() -> None:
    llm = _Claude([_text("Hi."), _final()])
    model = model_for(cast("AnthropicAdapterClass", llm), "Haiku 5.5", [])

    assert (await _one_round(model))[-1] == RoundDone([], None)
