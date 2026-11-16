import json
from types import SimpleNamespace
from typing import cast

import pytest
from anthropic import AsyncAnthropic, Omit
from anthropic.types import ParsedTextBlock, TextBlock, ThinkingBlock, ToolUseBlock
from pydantic import BaseModel

from app.adapters.anthropic_adp import AnthropicAdapterClass, AnthropicRefusalError


class _LookupArgs(BaseModel):
    """Look up the weather in a city."""

    city: str


class _Answer(BaseModel):
    city: str
    result: str


def _tool_use(call_id: str = "toolu_123", city: object = "Bengaluru") -> ToolUseBlock:
    return ToolUseBlock(type="tool_use", id=call_id, name="lookup", input={"city": city})


def _message(*content: object, stop_reason: str, parsed: object = None) -> SimpleNamespace:
    return SimpleNamespace(
        content=list(content),
        stop_reason=stop_reason,
        stop_details=None,
        parsed_output=parsed,
    )


def _answer() -> SimpleNamespace:
    final = _Answer(city="Bengaluru", result="sunny")
    block = ParsedTextBlock[_Answer](
        type="text", text=final.model_dump_json(), citations=None, parsed_output=final
    )
    return _message(block, stop_reason="end_turn", parsed=final)


class _FakeMessages:
    def __init__(self, *responses: SimpleNamespace) -> None:
        self.calls: list[dict[str, object]] = []
        self._responses = list(responses)

    async def _next(self, **kwargs: object) -> object:
        # The adapter keeps appending to one history list, so each call is copied.
        kwargs["messages"] = list(cast("list[object]", kwargs["messages"]))
        self.calls.append(kwargs)
        return self._responses.pop(0)

    parse = _next
    create = _next
    count_tokens = _next


def _adapter(*responses: SimpleNamespace) -> tuple[AnthropicAdapterClass, _FakeMessages]:
    adapter = AnthropicAdapterClass()
    messages = _FakeMessages(*responses)
    adapter._client = cast("AsyncAnthropic", SimpleNamespace(messages=messages))
    return adapter, messages


def _history(request: dict[str, object]) -> list[dict[str, object]]:
    return cast("list[dict[str, object]]", request["messages"])


async def test_structured_response_runs_a_tool_then_parses_the_final_model() -> None:
    thinking = ThinkingBlock(type="thinking", thinking="", signature="sig_abc")
    adapter, messages = _adapter(_message(thinking, _tool_use(), stop_reason="tool_use"), _answer())
    received: list[_LookupArgs] = []

    async def lookup(arguments: _LookupArgs) -> dict[str, str]:
        received.append(arguments)
        return {"city": arguments.city, "result": "sunny"}

    response = await adapter.structured_response(
        model="Opus 5.5",
        messages="Look up Bengaluru",
        output_model=_Answer,
        tools=[adapter.Tools.pydantic_function(_LookupArgs, name="lookup")],
        tool_handlers={"lookup": lookup},
        argument_models={"lookup": _LookupArgs},
        initial_tool_choice={"type": "auto", "disable_parallel_tool_use": True},
    )

    assert response.parsed_output == _Answer(city="Bengaluru", result="sunny")
    assert received == [_LookupArgs(city="Bengaluru")]

    first, second = messages.calls
    assert first["model"] == "claude-opus-5-5"
    assert first["output_format"] is _Answer
    assert first["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert isinstance(second["tool_choice"], Omit)
    assert _history(first) == [{"role": "user", "content": "Look up Bengaluru"}]

    _, assistant, results = _history(second)
    # The whole reply goes back, thinking block and its signature included.
    assert assistant["role"] == "assistant"
    blocks = cast("list[dict[str, object]]", assistant["content"])
    assert [block["type"] for block in blocks] == ["thinking", "tool_use"]
    assert blocks[0]["signature"] == "sig_abc"
    assert results["role"] == "user"
    (result,) = cast("list[dict[str, object]]", results["content"])
    assert result["type"] == "tool_result"
    assert result["tool_use_id"] == "toolu_123"
    assert json.loads(cast("str", result["content"])) == {"city": "Bengaluru", "result": "sunny"}


async def test_structured_response_answers_every_call_of_a_round_in_one_message() -> None:
    adapter, messages = _adapter(
        _message(_tool_use("toolu_1"), _tool_use("toolu_2", "Pune"), stop_reason="tool_use"),
        _answer(),
    )

    await adapter.structured_response(
        model="Haiku 5.5",
        messages="Compare Bengaluru and Pune",
        output_model=_Answer,
        tool_handlers={"lookup": lambda arguments: f"sunny in {arguments['city']}"},
    )

    results = cast("list[dict[str, object]]", _history(messages.calls[1])[-1]["content"])
    assert [(r["tool_use_id"], r["content"]) for r in results] == [
        ("toolu_1", "sunny in Bengaluru"),
        ("toolu_2", "sunny in Pune"),
    ]


async def test_structured_response_tells_the_model_when_its_tool_input_is_invalid() -> None:
    adapter, messages = _adapter(_message(_tool_use(city=7), stop_reason="tool_use"), _answer())
    ran: list[object] = []

    await adapter.structured_response(
        model="Opus 5.5",
        messages="Look up Bengaluru",
        output_model=_Answer,
        tool_handlers={"lookup": ran.append},
        argument_models={"lookup": _LookupArgs},
    )

    assert ran == []
    (result,) = cast("list[dict[str, object]]", _history(messages.calls[1])[-1]["content"])
    assert result["is_error"] is True
    assert "Invalid input for lookup" in cast("str", result["content"])


async def test_structured_response_resumes_a_paused_server_tool_turn() -> None:
    paused = TextBlock(type="text", text="Searching", citations=None)
    adapter, messages = _adapter(_message(paused, stop_reason="pause_turn"), _answer())

    await adapter.structured_response(
        model="Opus 5.5",
        messages="Search for it",
        output_model=_Answer,
        tools=[adapter.Tools.web_search(max_uses=3)],
    )

    # The paused turn is sent back as it is, with no user message after it.
    assert [message["role"] for message in _history(messages.calls[1])] == ["user", "assistant"]


async def test_structured_response_does_not_send_the_parsed_field_back() -> None:
    preamble = _Answer(city="Bengaluru", result="checking")
    text = ParsedTextBlock[_Answer](
        type="text", text=preamble.model_dump_json(), citations=None, parsed_output=preamble
    )
    adapter, messages = _adapter(
        _message(text, _tool_use(), stop_reason="tool_use", parsed=preamble), _answer()
    )

    await adapter.structured_response(
        model="Opus 5.5",
        messages="Look up Bengaluru",
        output_model=_Answer,
        tool_handlers={"lookup": lambda _: "sunny"},
    )

    blocks = cast("list[dict[str, object]]", _history(messages.calls[1])[1]["content"])
    assert blocks[0]["type"] == "text"
    assert "parsed_output" not in blocks[0]


async def test_structured_response_requires_a_handler_for_custom_tools() -> None:
    adapter, _ = _adapter(_message(_tool_use(), stop_reason="tool_use"))

    with pytest.raises(KeyError, match="No tool handler registered for 'lookup'"):
        await adapter.structured_response(
            model="Opus 5.5", messages="Look up Bengaluru", output_model=_Answer
        )


async def test_structured_response_never_runs_a_tool_call_cut_off_by_max_tokens() -> None:
    adapter, _ = _adapter(_message(_tool_use(), stop_reason="max_tokens"))
    ran: list[object] = []

    with pytest.raises(RuntimeError, match="ran out of max_tokens"):
        await adapter.structured_response(
            model="Opus 5.5",
            messages="Look up Bengaluru",
            output_model=_Answer,
            tool_handlers={"lookup": ran.append},
        )
    assert ran == []


async def test_structured_response_stops_after_max_tool_rounds() -> None:
    adapter, messages = _adapter(
        _message(_tool_use("toolu_1"), stop_reason="tool_use"),
        _message(_tool_use("toolu_2"), stop_reason="tool_use"),
    )

    with pytest.raises(RuntimeError, match="exceeded 1 tool rounds"):
        await adapter.structured_response(
            model="Opus 5.5",
            messages="Look up Bengaluru",
            output_model=_Answer,
            tool_handlers={"lookup": lambda _: "sunny"},
            max_tool_rounds=1,
        )
    assert len(messages.calls) == 2


async def test_a_refusal_raises_with_its_category() -> None:
    refusal = _message(stop_reason="refusal")
    refusal.stop_details = SimpleNamespace(category="cyber", explanation="declined")
    adapter, _ = _adapter(refusal, refusal)

    with pytest.raises(AnthropicRefusalError) as raised:
        await adapter.text_response(model="Opus 5.5", messages="hello")
    assert raised.value.category == "cyber"

    with pytest.raises(AnthropicRefusalError):
        await adapter.structured_response(model="Opus 5.5", messages="hello", output_model=_Answer)


async def test_text_response_joins_the_text_and_skips_thinking() -> None:
    adapter, messages = _adapter(
        _message(
            ThinkingBlock(type="thinking", thinking="", signature="sig"),
            TextBlock(type="text", text="Hello, ", citations=None),
            TextBlock(type="text", text="world.", citations=None),
            stop_reason="end_turn",
        )
    )

    text = await adapter.text_response(
        model="Haiku 5.5",
        messages="Say hello",
        system="Be brief.",
        max_tokens=200,
        thinking={"type": "disabled"},
    )

    assert text == "Hello, world."
    (request,) = messages.calls
    assert request["model"] == "claude-haiku-5-5"
    assert request["messages"] == [{"role": "user", "content": "Say hello"}]
    assert request["system"] == "Be brief."
    assert request["max_tokens"] == 200
    assert request["thinking"] == {"type": "disabled"}


async def test_count_tokens_sends_the_same_conversation_shape() -> None:
    adapter, messages = _adapter(SimpleNamespace(input_tokens=12))

    counted = await adapter.count_tokens(model="Sonnet 5.5", messages="Say hello")

    assert counted.input_tokens == 12
    assert messages.calls[0]["model"] == "claude-sonnet-5-5"
    assert messages.calls[0]["messages"] == [{"role": "user", "content": "Say hello"}]


def test_stream_message_passes_the_request_to_the_sdk() -> None:
    adapter = AnthropicAdapterClass()
    opened: list[dict[str, object]] = []

    def stream(**kwargs: object) -> str:
        opened.append(kwargs)
        return "manager"

    adapter._client = cast(
        "AsyncAnthropic", SimpleNamespace(messages=SimpleNamespace(stream=stream))
    )
    tool = adapter.Tools.function(name="lookup", description="Look up.", parameters={})

    manager = adapter.stream_message(
        model="Fable 5.2",
        messages=[{"role": "user", "content": "hi"}],
        tools=[tool],
        tool_choice={"type": "none"},
        cache_control={"type": "ephemeral"},
    )

    assert cast("object", manager) == "manager"
    (request,) = opened
    assert request["model"] == "claude-fable-5-2"
    assert request["max_tokens"] == 64_000
    assert request["tools"] == [tool]
    assert request["tool_choice"] == {"type": "none"}
    assert request["cache_control"] == {"type": "ephemeral"}
    assert isinstance(request["thinking"], Omit)


def test_tool_builders() -> None:
    tools = AnthropicAdapterClass.Tools
    schema: dict[str, object] = {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
        "additionalProperties": False,
    }

    assert tools.function(name="lookup", description="Look up.", parameters=schema) == {
        "name": "lookup",
        "description": "Look up.",
        "input_schema": schema,
        "strict": True,
    }
    eager = tools.function(
        name="lookup", description="Look up.", parameters=schema, eager_input_streaming=True
    )
    assert eager.get("eager_input_streaming") is True

    from_model = tools.pydantic_function(_LookupArgs, name="lookup")
    assert from_model.get("strict") is True
    assert from_model.get("description") == "Look up the weather in a city."
    input_schema = cast("dict[str, object]", from_model["input_schema"])
    assert input_schema["additionalProperties"] is False
    assert input_schema["required"] == ["city"]

    assert tools.web_search() == {"type": "web_search_20260209", "name": "web_search"}
    assert tools.web_search(max_uses=2, allowed_domains=["example.com"]) == {
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": 2,
        "allowed_domains": ["example.com"],
    }
    assert tools.web_fetch(citations=True, max_content_tokens=5000) == {
        "type": "web_fetch_20260209",
        "name": "web_fetch",
        "max_content_tokens": 5000,
        "citations": {"enabled": True},
    }
    assert tools.code_execution() == {"type": "code_execution_20260521", "name": "code_execution"}


def test_build_text_input_appends_the_prompt() -> None:
    assert AnthropicAdapterClass.build_text_input(
        [("user", "hi"), ("assistant", "hello")], "how are you?"
    ) == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "how are you?"},
    ]


async def test_the_client_needs_an_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = AnthropicAdapterClass()
    monkeypatch.setattr("app.adapters.anthropic_adp.settings.anthropic_api_key", "")
    await adapter.initialize()
    await adapter.close()

    with pytest.raises(RuntimeError, match="API key not configured"):
        _ = adapter.client

    monkeypatch.setattr("app.adapters.anthropic_adp.settings.anthropic_api_key", "sk-ant-test")
    await adapter.initialize()
    assert adapter.client.api_key == "sk-ant-test"
    await adapter.close()


def test_stream_message_without_tools_sends_no_tool_choice_either() -> None:
    adapter = AnthropicAdapterClass()
    opened: list[dict[str, object]] = []

    def stream(**kwargs: object) -> None:
        opened.append(kwargs)

    adapter._client = cast(
        "AsyncAnthropic", SimpleNamespace(messages=SimpleNamespace(stream=stream))
    )
    adapter.stream_message(
        model="Haiku 5.5",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        tool_choice={"type": "auto"},
    )

    (request,) = opened
    assert isinstance(request["tools"], Omit)
    assert isinstance(request["tool_choice"], Omit)
