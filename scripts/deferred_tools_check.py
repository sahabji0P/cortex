"""Check deferred tools on a real model: the search, the loading, and the prompt cache.

Run from the repository root (needs the model's own key in .env):

    uv run python -m scripts.deferred_tools_check               # the default model, 6 Luna
    uv run python -m scripts.deferred_tools_check "Haiku 5.5"   # or any other model's name
    uv run python -m scripts.deferred_tools_check "6 Luna" proxy  # OpenAI through the proxy

No tool server is needed: twelve made-up tools of two connections, all deferred, answer
locally. The model, `model_for`, the toolbox and the reply loop are the real ones. Two
turns are run on one history: the first needs a deferred tool, so the model searches,
loads it and calls it; the second needs the same tool again, and must not search.

Each request prints its token counts: what was read in full, what was read from the
cache, and what was written to it, with a hash of the request's `tools`. The cache holds
when every request after the first reads at least what the one before it was.
"""

import asyncio
import contextlib
import hashlib
import json
import math
import sys
from typing import Any, cast

from app.adapters.anthropic_adp import AnthropicAdapterClass
from app.adapters.openai_adp import OpenAIAdapterClass
from app.entities.late_results import CANCEL_TOOL
from app.entities.loop import Reply, RoundInput, run_reply
from app.entities.model import SEARCH_TOOL, Item, ToolSpec, is_openai, model_for
from app.entities.tools import Tool, gem_toolbox

MODEL = sys.argv[1] if len(sys.argv) > 1 else "6 Luna"
VIA = sys.argv[2] if len(sys.argv) > 2 else "api"

# Long enough to be cached on either provider: above 1,024 tokens for OpenAI and 4,096
# for the smallest Anthropic models. The same text every time.
_RULE = (
    "Rule {n}: when the user asks about work items, answer from what the tools return, "
    "name each item by its number, keep the answer to two short sentences, and never "
    "guess at an item's state when a tool can tell you."
)
INSTRUCTIONS = "You help a small team keep track of its work. Be brief.\n" + "\n".join(
    _RULE.format(n=n) for n in range(1, 140)
)

_ISSUES = {
    "login": [{"number": 101, "title": "Login fails after a password reset", "state": "open"}],
    "payment": [{"number": 202, "title": "Payment page times out", "state": "open"}],
}


def _tool(connection: str, name: str, description: str, **arguments: str) -> Tool:
    async def run(given: str) -> str:
        if name == "find_issues":
            text = str(json.loads(given).get("query", "")).lower()
            return json.dumps([i for key, found in _ISSUES.items() if key in text for i in found])
        return "done"

    spec = ToolSpec(
        name=f"{connection.lower()}__{name}",
        description=description,
        parameters={
            "type": "object",
            "properties": {
                key: {"type": "string", "description": d} for key, d in arguments.items()
            },
        },
        strict=False,
        deferred=True,
    )
    return Tool(spec, run, connection=connection)


TOOLS = [
    _tool(
        "Tracker",
        "find_issues",
        "Find issues whose text mentions a query.",
        query="Words to look for.",
    ),
    _tool("Tracker", "get_issue", "Read one issue in full.", number="The issue's number."),
    _tool("Tracker", "close_issue", "Close an issue.", number="The issue's number."),
    _tool("Tracker", "reopen_issue", "Reopen a closed issue.", number="The issue's number."),
    _tool(
        "Tracker",
        "comment",
        "Add a comment to an issue.",
        number="Its number.",
        text="The comment.",
    ),
    _tool("Tracker", "assign", "Assign an issue to a person.", number="Its number.", person="Who."),
    _tool("Tracker", "label", "Put a label on an issue.", number="Its number.", label="The label."),
    _tool("Tracker", "list_projects", "List the tracker's projects."),
    _tool("Calendar", "list_events", "List calendar events in a range.", start="From.", end="To."),
    _tool(
        "Calendar", "create_event", "Create a calendar event.", title="Its title.", start="When."
    ),
    _tool("Calendar", "delete_event", "Delete a calendar event.", event="Its id."),
    _tool("Calendar", "free_slots", "Find free time in the calendar.", day="The day."),
]


class _NoSearch:
    configured = False


# Web search is not offered: a stand-in that says it is not set up.
_NO_SEARCH = cast("Any", _NoSearch())


async def _cancel(arguments: str) -> str:
    return "Nothing is running."


class _Part:
    def __init__(self) -> None:
        self.text = ""

    def delivered(self, at: float) -> tuple[str, bool]:
        return self.text, True


class _Surface:
    """Text, delivered as soon as it is said."""

    def __init__(self) -> None:
        self._part: _Part | None = None

    async def say(self, text: str) -> _Part:
        if self._part is None:
            self._part = _Part()
        self._part.text += text
        return self._part

    async def end_part(self) -> None:
        self._part = None

    async def finish(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class Tap:
    """The adapter, writing down each request's tools and token counts."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.rows: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def _row(self, request: dict[str, Any]) -> dict[str, Any]:
        tools = json.dumps(request.get("tools"), sort_keys=True)
        row = {"tools": hashlib.sha256(tools.encode()).hexdigest()[:12], "usage": None}
        self.rows.append(row)
        return row

    async def stream_response(self, **request: Any) -> Any:
        row = self._row(request)
        stream = await self._real.stream_response(**request)

        async def tap():
            async with contextlib.aclosing(stream):
                async for event in stream:
                    if event.type == "response.completed" and event.response.usage:
                        usage = event.response.usage
                        cached = usage.input_tokens_details.cached_tokens
                        row["usage"] = (usage.input_tokens - cached, cached, 0)
                    yield event

        return tap()

    def stream_message(self, **request: Any) -> Any:
        return _Tapped(self._real.stream_message(**request), self._row(request))


class _Tapped:
    def __init__(self, manager: Any, row: dict[str, Any]) -> None:
        self._manager, self._row, self._stream = manager, row, None

    async def __aenter__(self) -> "_Tapped":
        self._stream = await self._manager.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> Any:
        return await self._manager.__aexit__(*exc)

    def __aiter__(self) -> Any:
        return self._stream.__aiter__()

    async def get_final_message(self) -> Any:
        message = await self._stream.get_final_message()
        usage = message.usage
        read = usage.cache_read_input_tokens or 0
        written = usage.cache_creation_input_tokens or 0
        self._row["usage"] = (usage.input_tokens, read, written)
        return message


async def _turn(llm: Tap, history: list[Item], text: str) -> list[str]:
    """One user turn and the gem's reply. Returns the tools it called, in order."""
    history.append({"role": "user", "content": text})
    toolbox = gem_toolbox((), _NO_SEARCH, Tool(CANCEL_TOOL, _cancel), history, TOOLS)
    model = model_for(cast("Any", llm), MODEL, toolbox.specs)
    reply = Reply(_Surface())
    words: list[str] = []
    await run_reply(
        reply,
        model,
        toolbox,
        instructions=INSTRUCTIONS + "\n" + model.own_instructions,
        next_round=lambda: RoundInput(list(history), False),
        said=words.append,
        show=lambda shown: None,
    )
    calls = reply.calls()
    for call in calls:
        print(f"    called {call.name} {call.arguments} -> {call.output}")
    history.extend(reply.delivered(math.inf).items)
    print(f"    answer: {''.join(words).strip()!r}")
    return [call.name for call in calls]


def _payload_sizes() -> tuple[int, int]:
    """The `tools` of a request, in characters, with the twelve tools deferred and not."""
    cancel = Tool(CANCEL_TOOL, _cancel)
    deferred = gem_toolbox((), _NO_SEARCH, cancel, [], TOOLS).specs
    flat = [
        ToolSpec(s.name, s.description, s.parameters, s.strict)
        for s in deferred
        if s.name != SEARCH_TOOL
    ]
    sizes = []
    for specs in (deferred, flat):
        if is_openai(MODEL):
            model: Any = model_for(OpenAIAdapterClass(), MODEL, specs)
        else:
            model = model_for(AnthropicAdapterClass(), MODEL, specs)
        sizes.append(len(json.dumps(model._tools)))
    return sizes[0], sizes[1]


async def main() -> None:
    real: Any = OpenAIAdapterClass() if is_openai(MODEL) else AnthropicAdapterClass()
    await real.initialize()
    if is_openai(MODEL):
        real.use(VIA)
    llm = Tap(real)
    history: list[Item] = []
    print(f"{MODEL}" + (f" via {VIA}" if is_openai(MODEL) else ""))
    failed = []
    try:
        print("turn 1: needs a deferred tool")
        first = await _turn(
            llm, history, "Which open issues are there about login? Use the tracker."
        )
        if first[:1] != [SEARCH_TOOL] or "tracker__find_issues" not in first:
            failed.append("turn 1 did not search and then call tracker__find_issues")
        print("turn 2: the same tool again")
        second = await _turn(llm, history, "And which are about payment?")
        if SEARCH_TOOL in second:
            failed.append("turn 2 searched again")
        if "tracker__find_issues" not in second:
            failed.append("turn 2 did not call tracker__find_issues")
    finally:
        await real.close()

    print("requests (read in full, read from cache, written to cache, tools hash):")
    for n, row in enumerate(llm.rows, 1):
        print(f"    {n}: {row['usage']}  tools {row['tools']}")
    if len({row["tools"] for row in llm.rows}) != 1:
        failed.append("the tools payload changed between requests")
    # Every request starts with the whole of the one before, so from the second on it
    # should read at least what the one before it read or wrote.
    pairs = zip(llm.rows, llm.rows[1:], strict=False)
    for n, (before, after) in enumerate(pairs, 2):
        held = before["usage"] and before["usage"][1] + before["usage"][2]
        if after["usage"] and held and after["usage"][1] < held:
            failed.append(f"request {n} read less from the cache than request {n - 1} held")
    deferred, flat = _payload_sizes()
    print(f"tools payload: {deferred} characters deferred, {flat} not deferred")
    print("PASS" if not failed else "CHECK: " + "; ".join(failed))


if __name__ == "__main__":
    asyncio.run(main())
