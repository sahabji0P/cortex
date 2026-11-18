"""
Tools a conversation's model may call, one small object each.

A `Tool` holds everything a conversation needs to know of one tool: what the model is
told of it (its `ToolSpec`), how to run a call of it, and how such a call is shown to the
client.

A `Toolbox` holds the tools one conversation can use. Which those are is decided when
the conversation opens, and the list is not changed after that: it is part of the start
of every request, and a prompt cache only helps while that start stays the same.
"""

import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from app.adapters.parallel_adp import ParallelAdapterClass, SearchResult
from app.entities.model import Item, ToolSpec

__all__ = ["GEM_TOOL_NAMES", "Tool", "Toolbox", "gem_toolbox", "web_search_tool"]

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
    key is set. `cancel_tool_call` is offered with any other tool. A name with no tool
    behind it here is passed over, so a config saved before a tool went still works.

    The order is always the same: web search, `cancel_tool_call`, the connections'
    tools, then the tools that are only declared (below).
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
    tools.extend(connected)
    offered = {tool.name for tool in tools}
    tools.extend(
        Tool(_gone(name), _not_available)
        for name in called
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
