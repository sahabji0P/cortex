"""The tools a conversation's model may call, without a conversation around them."""

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from app.entities.model import ToolSpec
from app.entities.tools import Tool, Toolbox, web_search_tool

if TYPE_CHECKING:
    from app.adapters.parallel_adp import ParallelAdapterClass


def _spec(name: str) -> ToolSpec:
    return ToolSpec(name=name, description=f"The {name} tool.", parameters={})


def _tool(name: str, *, answer: str = "done", fails: bool = False, shows: bool = False) -> Tool:
    async def run(arguments: str) -> str:
        if fails:
            raise RuntimeError("boom")
        return answer

    def shown(arguments: str) -> str | None:
        return f"running {name} with {arguments}" if shows else None

    return Tool(_spec(name), run, shown=shown)


class _Search:
    """The Parallel adapter: records each search and answers with the pages given."""

    def __init__(self, *pages: SimpleNamespace) -> None:
        self.searches: list[dict[str, object]] = []
        self._pages = list(pages)

    async def search(self, **request: object) -> SimpleNamespace:
        self.searches.append(request)
        return SimpleNamespace(results=self._pages)


def _page(**fields: object) -> SimpleNamespace:
    page = {"title": "Race report", "url": "https://example.com/race", "publish_date": None}
    return SimpleNamespace(**{**page, "excerpts": [], **fields})


# ---- the toolbox


def test_the_specs_keep_the_order_the_tools_were_given_in() -> None:
    toolbox = Toolbox([_tool("b"), _tool("a"), _tool("c")])

    assert [spec.name for spec in toolbox.specs] == ["b", "a", "c"]
    assert Toolbox().specs == []


def test_a_tool_is_found_by_name() -> None:
    tool = _tool("a")
    toolbox = Toolbox([tool])

    assert toolbox.get("a") is tool
    assert toolbox.get("b") is None


def test_two_tools_may_not_share_a_name() -> None:
    with pytest.raises(ValueError, match="two tools are called 'a'"):
        Toolbox([_tool("a"), _tool("a")])


async def test_a_call_runs_its_tool_and_returns_the_result() -> None:
    toolbox = Toolbox([_tool("a", answer="the result")])
    shown: list[str] = []

    assert await toolbox.run("a", "{}", show=shown.append) == "the result"
    # This tool sends no event.
    assert shown == []


async def test_a_call_is_shown_before_it_runs() -> None:
    happened: list[str] = []

    async def run(arguments: str) -> str:
        happened.append("ran")
        return ""

    toolbox = Toolbox([Tool(_spec("a"), run, shown=lambda arguments: f"shown {arguments}")])

    await toolbox.run("a", '{"x": 1}', show=happened.append)

    assert happened == ['shown {"x": 1}', "ran"]


async def test_a_failure_goes_back_to_the_model_as_text() -> None:
    toolbox = Toolbox([_tool("a", fails=True, shows=True)])
    shown: list[str] = []

    assert await toolbox.run("a", "{}", show=shown.append) == "a failed: boom"
    assert shown == ["running a with {}"]


async def test_a_call_of_a_tool_that_is_not_there_says_so() -> None:
    assert await Toolbox().run("web_search", "{}", show=print) == "Unknown tool: web_search"


# ---- searching the web


async def test_a_search_shows_its_query_and_returns_the_pages_as_text() -> None:
    search = _Search(
        _page(publish_date="2026-11-08", excerpts=["Norris won.", "Piastri was second."]),
        _page(title=None, url="https://example.com/other"),
    )
    toolbox = Toolbox([web_search_tool(cast("ParallelAdapterClass", search))])
    shown: list[str] = []

    output = await toolbox.run(
        "web_search", json.dumps({"query": "who won the race"}), show=shown.append
    )

    assert shown == ["who won the race"]
    # The fastest mode: the user waits in silence while it runs.
    assert search.searches == [
        {"objective": "who won the race", "queries": ["who won the race"], "mode": "turbo"}
    ]
    assert output == (
        "Race report (https://example.com/race)\n"
        "Published: 2026-11-08\n"
        "Norris won.\n"
        "Piastri was second.\n"
        "\n"
        "https://example.com/other (https://example.com/other)"
    )


async def test_a_search_that_finds_nothing_says_so() -> None:
    toolbox = Toolbox([web_search_tool(cast("ParallelAdapterClass", _Search()))])

    assert await toolbox.run("web_search", '{"query": "x"}', show=print) == "No results."


async def test_a_search_without_a_query_fails_without_being_shown_or_run() -> None:
    search = _Search()
    toolbox = Toolbox([web_search_tool(cast("ParallelAdapterClass", search))])
    shown: list[str] = []

    output = await toolbox.run("web_search", '{"q": "x"}', show=shown.append)

    assert output == "web_search failed: 'query'"
    assert shown == []
    assert search.searches == []


def test_search_results_may_be_cleared_and_the_spec_is_strict() -> None:
    tool = web_search_tool(cast("ParallelAdapterClass", _Search()))

    assert tool.name == "web_search"
    assert tool.clearable
    assert tool.spec.parameters["additionalProperties"] is False
    assert not _tool("a").clearable
