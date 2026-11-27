"""Deferred tools: found with `tool_search`, then loaded the way each provider loads them."""

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from app.entities.late_results import CANCEL_TOOL
from app.entities.model import SEARCH_TOOL, Item, ModelCall, RoundDone, ToolSpec, model_for
from app.entities.tools import Tool, gem_toolbox, search_tools

if TYPE_CHECKING:
    from app.adapters.anthropic_adp import AnthropicAdapterClass
    from app.adapters.openai_adp import OpenAIAdapterClass


async def _done(arguments: str) -> str:
    return "done"


def _tool(name: str, description: str = "", *, deferred: bool = True, **arguments: Any) -> Tool:
    spec = ToolSpec(
        name=name,
        description=description or f"The {name} tool.",
        parameters={"type": "object", "properties": arguments},
        strict=False,
        deferred=deferred,
    )
    return Tool(spec, _done, connection="Tracker" if deferred else "Notes")


_FIND = _tool("tracker__find_issues", "Find the issues that mention a text.", query={})
_CLOSE = _tool("tracker__close_issue", "Close an issue.", number={})
_NOTE = _tool("notes__add", "Add a note.", deferred=False)


class _NoSearch:
    configured = False


def _toolbox(history: list[Item], *connected: Tool) -> list[ToolSpec]:
    return gem_toolbox(
        (), cast("Any", _NoSearch()), Tool(CANCEL_TOOL, _done), history, connected
    ).specs


def _searched(*names: str, call_id: str = "s1") -> list[Item]:
    return [
        {"role": "user", "content": "Close issue 4."},
        {
            "type": "function_call",
            "call_id": call_id,
            "name": SEARCH_TOOL,
            "arguments": '{"query": "close issue"}',
        },
        {
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps({"tools": list(names)}),
        },
    ]


# ---- what a gem is offered


def test_a_gem_is_offered_tool_search_only_with_a_deferred_tool() -> None:
    assert [spec.name for spec in _toolbox([], _NOTE)] == ["cancel_tool_call", "notes__add"]

    specs = _toolbox([], _NOTE, _FIND, _CLOSE)

    assert [spec.name for spec in specs] == [
        "cancel_tool_call",
        SEARCH_TOOL,
        "notes__add",
        "tracker__find_issues",
        "tracker__close_issue",
    ]
    search = specs[1]
    assert not search.deferred
    assert "Tracker (2)" in search.description
    assert "Notes" not in search.description


async def test_a_search_shows_a_short_line_and_returns_the_names_it_found() -> None:
    toolbox = gem_toolbox(
        (), cast("Any", _NoSearch()), Tool(CANCEL_TOOL, _done), [], [_NOTE, _FIND, _CLOSE]
    )
    shown: list[str] = []

    output = await toolbox.run(SEARCH_TOOL, '{"query": "close an issue"}', show=shown.append)

    assert shown == ["Looking for a tool"]
    # The tool that is not deferred is never in a search's result.
    assert json.loads(output) == {"tools": ["tracker__close_issue", "tracker__find_issues"]}


def test_a_search_ranks_by_the_words_it_shares_caps_and_puts_a_name_first() -> None:
    many = [_tool(f"tracker__issue_{n}") for n in range(12)]

    assert search_tools("find issue", [_CLOSE, _FIND]) == [
        "tracker__find_issues",
        "tracker__close_issue",
    ]
    assert search_tools("number", [_FIND, _CLOSE]) == ["tracker__close_issue"]
    assert search_tools("weather", [_FIND, _CLOSE]) == []
    assert len(search_tools("issue", many)) == 8
    assert search_tools("tracker__issue_11", many)[0] == "tracker__issue_11"


def test_a_search_counts_a_rare_word_above_a_common_one() -> None:
    # Each tool has two of the three words, but most tools say "get" and only one says
    # "lock": a count alone would tie them and keep their order.
    tools = [
        _tool("tracker__get_issue", "Get an issue."),
        _tool("tracker__get_comments", "Get the comments on an issue."),
        _tool("tracker__get_labels", "Get the labels of an issue."),
        _tool("tracker__lock_issue", "Lock an issue."),
    ]

    assert search_tools("get issue lock", tools)[0] == "tracker__lock_issue"


def test_a_word_in_a_tool_s_name_counts_above_one_in_its_description() -> None:
    mentions = _tool("tracker__close_issue", "Close an issue. Its comments stay.")
    named = _tool("tracker__add_comment", "Add a note to an issue.")

    assert search_tools("comment", [mentions, named]) == [
        "tracker__add_comment",
        "tracker__close_issue",
    ]


def test_a_loaded_tool_that_has_gone_stays_declared_and_deferred() -> None:
    specs = _toolbox(_searched("tracker__gone"), _FIND)

    gone = specs[-1]
    assert (gone.name, gone.deferred) == ("tracker__gone", True)
    assert gone.description == "This tool is not available to you now."


# ---- Anthropic


class _Claude:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def stream_message(self, **request: Any) -> Any:
        self.requests.append(request)
        return _Stream()


class _Stream:
    async def __aenter__(self) -> "_Stream":
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass

    async def __aiter__(self):
        for _ in ():
            yield

    async def get_final_message(self) -> SimpleNamespace:
        return SimpleNamespace(content=[], stop_reason="end_turn")


async def _round(model: Any, items: list[Item]) -> list[Any]:
    return [event async for event in model.round(items, instructions="", think=False)]


async def test_anthropic_marks_deferred_tools_and_loads_them_by_reference() -> None:
    llm = _Claude()
    specs = _toolbox([], _NOTE, _FIND, _CLOSE)
    model = model_for(cast("AnthropicAdapterClass", llm), "Haiku 5.5", specs)

    await _round(model, [{"role": "user", "content": "Close issue 4."}])
    # A name not offered now would be refused, and is left out.
    await _round(model, _searched("tracker__close_issue", "tracker__unknown"))
    await _round(model, _searched())

    first, loaded, none = llm.requests
    tools = first["tools"]
    assert [tool.get("defer_loading", False) for tool in tools] == [False, False, False, True, True]
    assert not any("cache_control" in tool for tool in tools)
    assert loaded["tools"] == tools
    # Made again from a history that has loaded a tool, the tools are the same.
    assert _toolbox(_searched("tracker__close_issue"), _NOTE, _FIND, _CLOSE) == specs
    result = loaded["messages"][-1]["content"][0]
    assert result["content"] == [{"type": "tool_reference", "tool_name": "tracker__close_issue"}]
    assert none["messages"][-1]["content"][0]["content"] == "No tools were found."


# ---- OpenAI


class _OpenAI:
    """The OpenAI adapter: each call returns the next scripted output, and is written down."""

    def __init__(self, *outputs: list[SimpleNamespace]) -> None:
        self.requests: list[dict[str, Any]] = []
        self._outputs = list(outputs)

    async def stream_response(self, **request: Any):
        self.requests.append(request)
        output = self._outputs.pop(0) if self._outputs else []

        async def stream():
            for item in output:
                yield SimpleNamespace(type="response.output_item.added", item=item)
            yield SimpleNamespace(
                type="response.completed", response=SimpleNamespace(output=output)
            )

        return stream()


async def test_openai_searches_in_client_mode_and_gets_the_tools_where_they_load() -> None:
    search_call = SimpleNamespace(
        type="tool_search_call", call_id="s1", arguments={"query": "close issue"}
    )
    llm = _OpenAI([search_call])
    specs = _toolbox([], _NOTE, _FIND, _CLOSE)
    model = model_for(cast("OpenAIAdapterClass", llm), "6 Luna", specs)

    events = await _round(model, [{"role": "user", "content": "Close issue 4."}])
    await _round(model, _searched("tracker__close_issue", "notes__add"))

    assert events[-1] == RoundDone([ModelCall("s1", SEARCH_TOOL, '{"query": "close issue"}')])
    first, loaded = llm.requests
    # The deferred tools are not in the request's tools: the search sends them.
    assert [tool.get("name", tool["type"]) for tool in first["tools"]] == [
        "cancel_tool_call",
        "tool_search",
        "notes__add",
    ]
    assert first["tools"][1]["execution"] == "client"
    assert loaded["tools"] == first["tools"]
    _, call, output = loaded["input"]
    assert call == {
        "type": "tool_search_call",
        "execution": "client",
        "call_id": "s1",
        "status": "completed",
        "arguments": {"query": "close issue"},
    }
    assert output["type"] == "tool_search_output"
    assert output["call_id"] == "s1"
    # A tool that is not deferred is in the request's tools already.
    assert [(tool["name"], tool["defer_loading"]) for tool in output["tools"]] == [
        ("tracker__close_issue", True)
    ]
