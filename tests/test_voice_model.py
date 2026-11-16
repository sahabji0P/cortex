"""The model behind a voice conversation: choosing a provider, and the Anthropic side.

The OpenAI side is covered by `test_voice_conversation`, which runs every scenario
through it.
"""

import asyncio
import json
from types import SimpleNamespace
from typing import cast

import pytest

from app.adapters.anthropic_adp import AnthropicAdapterClass
from app.adapters.openai_adp import OpenAIAdapterClass
from app.entities import voice_ent
from app.entities.voice_ent import VoiceConversation, VoiceEntityClass
from app.entities.voice_model import (
    CallStarted,
    Item,
    ModelCall,
    RoundDone,
    TextDelta,
    ToolSpec,
    to_messages,
    voice_model,
)
from app.routers.voice.models import VoiceModels
from tests.test_voice_conversation import (
    _DELAY,
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
        self.labelled: list[dict] = []

    def stream_message(self, **request) -> _Stream:
        self.requests.append(request)
        self.streams.append(_Stream(self._rounds.pop(0)))
        return self.streams[-1]

    async def text_response(self, **request) -> str:
        self.labelled.append(request)
        return "the rest of the story"


def _conversation(llm: _Claude, model: str = "Haiku 5.5", search: _Search | None = None):
    stt, tts, clock = _STT(), _TTS(), _Clock()
    conversation = VoiceConversation(
        stt=stt, tts=tts, llm=llm, search=search or _Search(), model=model, clock=clock
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
    model = voice_model(cast("AnthropicAdapterClass", llm), "Sonnet 5.5", [_SEARCH])

    events = [
        event
        async for event in model.round(
            [{"role": "user", "content": "Who won?"}],
            instructions="Be brief.\n",
            think=True,
            must_answer=False,
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
    assert request["system"].startswith("Be brief.\n")
    assert "<note>" in request["system"]
    # The thinking setting is fixed for the model: asking to think does not change it.
    assert request["thinking"] == {"type": "between_tools"}
    assert request["tool_choice"] == {"type": "auto"}
    assert request["cache_control"] == {"type": "ephemeral"}
    (tool,) = request["tools"]
    assert tool["name"] == "web_search"
    assert tool["strict"] is True
    assert tool["input_schema"] == _SEARCH.parameters
    assert llm.streams[0].closed


async def test_a_round_that_must_answer_forbids_tools_without_changing_the_list() -> None:
    llm = _Claude([_text("Paris."), _final()], [_text("Paris."), _final()])
    model = voice_model(cast("AnthropicAdapterClass", llm), "Haiku 5.5", [_SEARCH])
    history: list[Item] = [{"role": "user", "content": "Capital of France?"}]

    for must_answer in (False, True):
        async for _ in model.round(history, instructions="", think=False, must_answer=must_answer):
            pass

    free, forced = llm.requests
    assert forced["tool_choice"] == {"type": "none"}
    assert forced["tools"] == free["tools"]
    assert forced["thinking"] == {"type": "disabled"}


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
    model = voice_model(cast("AnthropicAdapterClass", llm), "Haiku 5.5", [_SEARCH])

    with pytest.raises(RuntimeError, match=message):
        async for _ in model.round(
            [{"role": "user", "content": "hi"}], instructions="", think=False, must_answer=False
        ):
            pass


async def test_the_label_is_asked_of_the_small_model_without_thinking() -> None:
    llm = _Claude()
    model = voice_model(cast("AnthropicAdapterClass", llm), "Sonnet 5.5", [])

    assert await model.label("and then the wolf", instructions="Name it.") == (
        "the rest of the story"
    )
    assert llm.labelled == [
        {
            "model": "Haiku 5.5",
            "thinking": {"type": "disabled"},
            "messages": "and then the wolf",
            "system": "Name it.",
            "max_tokens": 30,
        }
    ]


# ---- choosing the provider


def test_a_model_needs_the_adapter_of_its_own_provider() -> None:
    with pytest.raises(TypeError, match="needs the Anthropic adapter"):
        voice_model(OpenAIAdapterClass(), "Haiku 5.5", [])
    with pytest.raises(TypeError, match="needs the OpenAI adapter"):
        voice_model(AnthropicAdapterClass(), "6 Luna", [])


@pytest.mark.parametrize(
    ("asked", "configured", "used", "provider"),
    [
        (None, "6 Luna", "6 Luna", "openai"),
        (None, "Sonnet 5.5", "Sonnet 5.5", "anthropic"),
        ("Haiku 5.5", "6 Luna", "Haiku 5.5", "anthropic"),
        ("6 Luna", "Haiku 5.5", "6 Luna", "openai"),
    ],
)
async def test_a_conversation_is_opened_on_the_model_asked_for_or_the_configured_one(
    monkeypatch: pytest.MonkeyPatch,
    asked: str | None,
    configured: str,
    used: str,
    provider: str,
) -> None:
    built: list[tuple[object, str]] = []
    real = voice_ent.voice_model

    def spy(llm, name, tools):
        built.append((llm, name))
        return real(llm, name, tools)

    monkeypatch.setattr(voice_ent, "voice_model", spy)
    monkeypatch.setattr(voice_ent.settings, "voice_model", configured)
    adapters = {"openai": OpenAIAdapterClass(), "anthropic": AnthropicAdapterClass()}
    stt = _STT()
    entity = VoiceEntityClass(
        stt=SimpleNamespace(connect_auto_turns=lambda: _connected(stt)),
        tts=_TTS(),
        openai=adapters["openai"],
        anthropic=adapters["anthropic"],
        search=_Search(),
    )

    conversation = await entity.open_conversation(asked)
    await conversation.close()

    assert built == [(adapters[provider], used)]


async def _connected(stt: _STT) -> _STT:
    return stt


def test_the_websocket_query_names_a_model_or_none() -> None:
    query = VoiceModels.V1.Input.Query
    assert query.model_validate({}).model is None
    assert query.model_validate({"model": "Haiku 5.5", "other": "x"}).model == "Haiku 5.5"
    with pytest.raises(ValueError, match="model"):
        query.model_validate({"model": "Opus 5.5"})


def test_the_websocket_query_says_who_speaks_first_and_defaults_to_the_user() -> None:
    query = VoiceModels.V1.Input.Query
    assert query.model_validate({}).first == "user"
    assert query.model_validate({"first": "assistant"}).first == "assistant"
    with pytest.raises(ValueError, match="first"):
        query.model_validate({"first": "nobody"})


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


# ---- a whole conversation on an Anthropic model


async def test_a_search_runs_between_two_model_calls_on_an_anthropic_model() -> None:
    llm = _Claude(
        [_call_starts(), _final(("toolu_1", "who won the race"))],
        [_text("Norris won."), _final()],
    )
    conversation, stt, _ = _conversation(llm)

    stt.say("who won the race?")
    outputs = await _read_until(conversation, "assistant.done")
    await conversation.close()

    assert outputs[-1].text == "Let me check. Norris won."
    first, second = llm.requests
    assert first["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "who won the race?"}]}
    ]
    _, assistant, result = second["messages"]
    assert assistant == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Let me check."},
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
        {"role": "user", "content": [{"type": "text", "text": "tell me a story"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Once upon…"}]},
        {"role": "user", "content": [{"type": "text", "text": "stop"}]},
    ]
