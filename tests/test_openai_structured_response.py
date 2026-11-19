import json
from types import SimpleNamespace
from typing import cast

import pytest
from openai import AsyncOpenAI, Omit
from pydantic import BaseModel

from app.adapters.openai_adp import OpenAIAdapterClass


class _LookupArgs(BaseModel):
    city: str


class _Answer(BaseModel):
    city: str
    result: str


class _FunctionCall:
    type = "function_call"
    name = "lookup"
    call_id = "call_123"
    arguments = '{"city":"Bengaluru"}'
    parsed_arguments = _LookupArgs(city="Bengaluru")

    @staticmethod
    def to_dict() -> dict[str, object]:
        return {
            "type": "function_call",
            "name": "lookup",
            "call_id": "call_123",
            "arguments": '{"city":"Bengaluru"}',
            "parsed_arguments": {"city": "Bengaluru"},
        }


class _FakeResponses:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self._responses = [
            SimpleNamespace(
                output=[_FunctionCall()],
                output_parsed=None,
                status="completed",
            ),
            SimpleNamespace(
                output=[],
                output_parsed=_Answer(city="Bengaluru", result="sunny"),
                status="completed",
            ),
        ]

    async def parse(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self) -> None:
        self.responses = _FakeResponses()


@pytest.mark.asyncio
async def test_structured_response_runs_pydantic_tool_then_parses_final_model() -> None:
    adapter = OpenAIAdapterClass()
    fake_client = _FakeClient()
    adapter._client = cast("AsyncOpenAI", fake_client)
    received: list[_LookupArgs] = []

    async def lookup(arguments: _LookupArgs) -> dict[str, str]:
        received.append(arguments)
        return {"city": arguments.city, "result": "sunny"}

    tool = adapter.Tools.pydantic_function(_LookupArgs, name="lookup")
    response = await adapter.structured_response(
        model="Luna",
        input="Look up Bengaluru",
        output_model=_Answer,
        tools=[tool],
        tool_handlers={"lookup": lookup},
        initial_tool_choice={"type": "function", "name": "lookup"},
    )

    assert response.output_parsed == _Answer(city="Bengaluru", result="sunny")
    assert received == [_LookupArgs(city="Bengaluru")]
    assert len(fake_client.responses.calls) == 2

    first_request, second_request = fake_client.responses.calls
    assert first_request["text_format"] is _Answer
    assert first_request["store"] is False
    assert first_request["include"] == ["reasoning.encrypted_content"]
    assert first_request["tool_choice"] == {"type": "function", "name": "lookup"}
    assert isinstance(second_request["tool_choice"], Omit)

    second_input = cast("list[dict[str, object]]", second_request["input"])
    assert second_input[-2]["type"] == "function_call"
    assert "parsed_arguments" not in second_input[-2]
    assert second_input[-1]["type"] == "function_call_output"
    assert second_input[-1]["call_id"] == "call_123"
    assert json.loads(cast("str", second_input[-1]["output"])) == {
        "city": "Bengaluru",
        "result": "sunny",
    }


@pytest.mark.asyncio
async def test_structured_response_requires_a_handler_for_custom_tools() -> None:
    adapter = OpenAIAdapterClass()
    adapter._client = cast("AsyncOpenAI", _FakeClient())

    with pytest.raises(KeyError, match="No tool handler registered for 'lookup'"):
        await adapter.structured_response(
            model="Luna",
            input="Look up Bengaluru",
            output_model=_Answer,
            tools=[adapter.Tools.pydantic_function(_LookupArgs, name="lookup")],
        )


@pytest.mark.asyncio
async def test_each_way_set_up_gets_a_client_and_the_proxy_is_used_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = "app.adapters.openai_adp.settings"
    monkeypatch.setattr(f"{settings}.openai_api_key", "sk-openai")
    monkeypatch.setattr(f"{settings}.openai_proxy_url", "http://127.0.0.1:8317/v1")
    monkeypatch.setattr(f"{settings}.openai_proxy_key", "proxy-key")

    adapter = OpenAIAdapterClass()
    await adapter.initialize()
    assert (adapter.via, adapter.client.api_key) == ("proxy", "proxy-key")
    assert str(adapter.client.base_url) == "http://127.0.0.1:8317/v1/"

    adapter.use("api")
    assert (adapter.via, adapter.client.api_key) == ("api", "sk-openai")
    assert str(adapter.client.base_url) == "https://api.openai.com/v1/"
    await adapter.close()

    # Only the key: no proxy to switch to.
    monkeypatch.setattr(f"{settings}.openai_proxy_url", "")
    adapter = OpenAIAdapterClass()
    await adapter.initialize()
    assert adapter.via == "api"
    assert not adapter.is_set_up("proxy")
    with pytest.raises(ValueError, match="not set up"):
        adapter.use("proxy")
    await adapter.close()

    # Nothing set up.
    monkeypatch.setattr(f"{settings}.openai_api_key", "")
    adapter = OpenAIAdapterClass()
    await adapter.initialize()
    assert (adapter.via, adapter.configured) == ("api", False)
