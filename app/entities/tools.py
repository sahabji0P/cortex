"""
Tools a conversation's model may call, one small object each.

A `Tool` holds everything a conversation needs to know of one tool: what the model is
told of it (its `ToolSpec`), how to run a call of it, and how such a call is shown to the
client.

A `Toolbox` holds the tools one conversation can use. Which those are is decided when
the conversation opens, and the list is not changed after that: it is part of the start
of every request, and a prompt cache only helps while that start stays the same.

A connection's tools can be deferred: the model is not told of them up front, and finds
and loads them with `tool_search` (see `model.py`). A tool it loads stays loaded, since
the search's result stays in the history.
"""

import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import cast

from app.adapters.parallel_adp import ParallelAdapterClass, SearchResult
from app.entities.model import SEARCH_TOOL, Item, ToolSpec, found

__all__ = [
    "GEM_TOOL_NAMES",
    "Tool",
    "Toolbox",
    "gem_toolbox",
    "search_tools",
    "tool_search_tool",
    "web_search_tool",
]

# The tools a gem can be given, by name. A gem's config is checked against this when it
# is saved. Each is offered on a call only when it can run there.
GEM_TOOL_NAMES: tuple[str, ...] = ("web_search",)


def _not_shown(arguments: str) -> str | None:
    return None


@dataclass(frozen=True, slots=True)
class Tool:
    """One tool: what the model is told of it, and what a call of it does."""

    spec: ToolSpec
    # Runs one call: its arguments as a JSON string in, the result for the model out.
    # It may raise. The failure then goes back to the model as text.
    run: Callable[[str], Awaitable[str]]
    # The text of the `tool.call` event a call sends the client, from its arguments, or
    # None to send no event. It is worked out just before the call runs, and may raise
    # the same way.
    shown: Callable[[str], str | None] = _not_shown
    # The name of the connection it is of, if it is one of a connection's tools.
    connection: str | None = None

    @property
    def name(self) -> str:
        return self.spec.name


class Toolbox:
    """The tools one conversation can use, in the order the model is told of them."""

    def __init__(self, tools: Sequence[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"two tools are called {tool.name!r}")
            self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        """The tool called `name`, or None if there is no such tool here."""
        return self._tools.get(name)

    @property
    def specs(self) -> list[ToolSpec]:
        """What the model is told of each tool, always in the same order."""
        return [tool.spec for tool in self._tools.values()]

    async def run(self, name: str, arguments: str, *, show: Callable[[str], None]) -> str:
        """Run one call. A failure goes back to the model as text, so it can say so.

        `show` is given the text of the call's `tool.call` event, if it has one, just
        before the call runs. Arguments that cannot be read send no event.
        """
        tool = self._tools.get(name)
        if tool is None:
            return f"Unknown tool: {name}"
        try:
            if (shown := tool.shown(arguments)) is not None:
                show(shown)
            return await tool.run(arguments)
        except Exception as exc:
            return f"{name} failed: {exc}"


def gem_toolbox(
    names: Sequence[str],
    search: ParallelAdapterClass,
    cancel: Tool,
    history: Sequence[Item],
    connected: Sequence[Tool] = (),
) -> Toolbox:
    """The tools a gem is offered, the same in a call and in writing.

    `names` are the tools the gem's config names, `cancel` is `cancel_tool_call` for
    the gem's calls that outlive their replies, `history` is the conversation the tools
    are offered in, and `connected` are the tools of the connections the gem may use
    (see `connection_ent.py`). Web search is offered when the config names it and its
    key is set. `cancel_tool_call` is offered with any other tool. `tool_search` is
    offered when any connected tool is deferred. A name with no tool behind it here is
    passed over, so a config saved before a tool went still works.

    The order is always the same: web search, `cancel_tool_call`, `tool_search`, the
    connections' tools, then the tools that are only declared (below).
    """
    search_tool = web_search_tool(search)
    searches = search.configured and "web_search" in names
    called = _called(history)
    tools: list[Tool] = []
    # The conversation may hold calls of a tool the gem is no longer offered: one it
    # could search with, or a connection that has gone or been closed to it. A provider
    # refuses a history with calls of a tool it is not told of, so such a tool stays
    # declared, and a call of it now is answered with why not.
    if searches:
        tools.append(search_tool)
    elif search_tool.name in called:
        tools.append(Tool(search_tool.spec, _not_available))
    if searches or connected:
        tools.append(cancel)
    elif cancel.name in called:
        tools.append(Tool(cancel.spec, _not_available))
    deferred = [tool for tool in connected if tool.spec.deferred]
    # Kept once it has been searched with, even with nothing behind it now: a provider
    # needs it declared to read the searches in the history.
    if deferred or SEARCH_TOOL in called:
        tools.append(tool_search_tool(deferred))
    tools.extend(connected)
    offered = {tool.name for tool in tools}
    tools.extend(
        Tool(_gone(name), _not_available)
        for name in called
        if name not in offered and _TOOL_NAME.fullmatch(name)
    )
    # Likewise a tool a search loaded and that is not offered now: a search's result
    # that names a tool it is not told of is refused. It stays deferred, as it was.
    offered = {tool.name for tool in tools}
    tools.extend(
        Tool(replace(_gone(name), deferred=True), _not_available)
        for name in _loaded(history)
        if name not in offered and _TOOL_NAME.fullmatch(name)
    )
    return Toolbox(tools)


# What either provider takes as a tool's name. A call of anything else in the history
# was never of a tool, and cannot be declared.
_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _called(history: Sequence[Item]) -> list[str]:
    """The names of the tools called in a history, each once, in the order first called."""
    names: dict[str, None] = {}
    for item in history:
        if "type" in item and item["type"] == "function_call":
            names.setdefault(item["name"])
    return list(names)


def _loaded(history: Sequence[Item]) -> list[str]:
    """The names of the tools the searches in a history loaded, each once."""
    searches: set[str] = set()
    names: dict[str, None] = {}
    for item in history:
        if "type" not in item:
            continue
        if item["type"] == "function_call" and item["name"] == SEARCH_TOOL:
            searches.add(item["call_id"])
        elif item["type"] == "function_call_output" and item["call_id"] in searches:
            names.update(dict.fromkeys(found(item["output"]) or []))
    return list(names)


def _gone(name: str) -> ToolSpec:
    """What the model is told of a tool it called once and is not offered now."""
    return ToolSpec(
        name=name,
        description="This tool is not available to you now.",
        parameters={"type": "object", "properties": {}},
        strict=False,
    )


async def _not_available(arguments: str) -> str:
    return "This tool is not available to you now."


def web_search_tool(search: ParallelAdapterClass) -> Tool:
    """Searching the web through Parallel. The client is shown the query."""

    async def run(arguments: str) -> str:
        query = _query(arguments)
        # The fastest mode: the user is waiting in silence while this runs.
        found = await search.search(objective=query, queries=[query], mode="turbo")
        return _format_search(found)

    return Tool(_WEB_SEARCH, run, shown=_query)


def _query(arguments: str) -> str:
    return str(json.loads(arguments)["query"])


_WEB_SEARCH = ToolSpec(
    name="web_search",
    description=(
        "Search the web for current or factual information. Never call this for something "
        "a search is already running for: that result arrives by itself."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to look up, as a full question."}
        },
        "required": ["query"],
        "additionalProperties": False,
    },
)


def _format_search(result: SearchResult) -> str:
    """Render search results as plain text for the model."""
    if not result.results:
        return "No results."
    pages = [
        "\n".join(
            [
                f"{page.title or page.url} ({page.url})",
                *([f"Published: {page.publish_date}"] if page.publish_date else []),
                *page.excerpts,
            ]
        )
        for page in result.results
    ]
    return "\n\n".join(pages)


# The most tools one search loads.
_FOUND_MAX = 8


def tool_search_tool(deferred: Sequence[Tool]) -> Tool:
    """Finding and loading the `deferred` tools. The client is shown that a tool is looked for.

    Its description says what is behind it, by connection: on some providers a deferred
    tool is not shown to the model at all until it is loaded. It is part of the start of
    every request, so it is made the same way every time.
    """
    counts: dict[str, int] = {}
    for tool in deferred:
        if tool.connection is not None:
            counts[tool.connection] = counts.get(tool.connection, 0) + 1
    behind = "; ".join(f"{name} ({count})" for name, count in counts.items()) or "none now"
    spec = ToolSpec(
        name=SEARCH_TOOL,
        description=(
            "Find and load more tools. Some of your tools are not listed until you search "
            f"for them. Tools behind this search, by connection: {behind}. Search with a "
            "few words of what you need to do. A tool found is yours to call for the rest "
            "of the conversation: do not search for it again."
        ),
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What the tool should do."}},
            "required": ["query"],
            "additionalProperties": False,
        },
    )

    async def run(arguments: str) -> str:
        return json.dumps({"tools": search_tools(_query(arguments), deferred)})

    return Tool(spec, run, shown=lambda arguments: "Looking for a tool")


def search_tools(query: str, tools: Sequence[Tool]) -> list[str]:
    """The names of the tools that best fit `query`, best first, at most `_FOUND_MAX`.

    A tool scores one for each word of the query found among the words of its name
    (without its connection's key), its description and its arguments' names (a word
    found as the start of one counts: "issue" finds "issues"). A query that is a tool's
    name finds that tool first. Ties keep the tools' own order, and a tool that scores
    nothing is not found.
    """
    asked = set(_words(query))
    scored: list[tuple[int, int, str]] = []
    for index, tool in enumerate(tools):
        if tool.name == query.strip():
            scored.append((-len(asked) - 1, index, tool.name))
            continue
        properties = tool.spec.parameters.get("properties")
        named = cast("dict[object, object]", properties) if isinstance(properties, dict) else {}
        arguments = " ".join(str(name) for name in named)
        # A connection's tool is named `{key}__{tool}`. The key is left out: it is in
        # every name of the connection, so a query that names the connection would find
        # all its tools, and load the eight first ones whatever was asked for.
        own = _words(f"{tool.name.split('__', 1)[-1]} {tool.spec.description} {arguments}")
        score = sum(1 for word in asked if any(one.startswith(word) for one in own))
        if score:
            scored.append((-score, index, tool.name))
    return [name for _, _, name in sorted(scored)[:_FOUND_MAX]]


def _words(text: str) -> list[str]:
    return [word for word in re.split(r"[^a-z0-9]+", text.lower()) if word]
