"""What a gem's conversation is sent with a compaction in force, and what its log says it was.

The compaction here is written straight into the log, as a compaction leaves it (see
`compaction.py`): how one is made is tested in `test_compactions.py`.
"""

import json
from datetime import UTC, datetime
from typing import Any

from bson import ObjectId

from app.entities.compaction import summed_up
from app.entities.history import Added
from app.entities.model import SEARCH_TOOL, ToolSpec, to_messages
from app.entities.tools import Tool
from app.routers.gems.service import GemsServiceV1Class
from tests.test_chat import _chat, _close, _open, _said
from tests.test_one_conversation import _hang_up, _said_and_heard, _spoken, _world
from tests.time_lines import TIME_LINE


async def _talked(*replies: str) -> Any:
    """A gem that has answered a message with each of `replies`, one after the other."""
    setup = await _chat(*(_said(reply) for reply in replies))
    socket, linked = await _open(setup.service())
    for count, _ in enumerate(replies, start=1):
        socket.send(setup.gem.id, f"message {count}")
        await socket.until("chat.done", count)
    setup.socket, setup.linked = socket, linked
    return setup


async def _log(setup: Any) -> list[dict[str, Any]]:
    service = GemsServiceV1Class(setup.gems, setup.conversations)
    return (await service.get_log(str(setup.gem.id), before=None, limit=500)).entries


async def _said_at(setup: Any, text: str) -> int:
    """The number of the entry that remembers the user saying `text`."""
    return next(
        entry["seq"]
        for entry in await _log(setup)
        if entry["kind"] == "user.said" and entry["item"]["content"] == text
    )


async def _compact(setup: Any, through: int, summary: str = "They talked.") -> None:
    gem_id = ObjectId(setup.gem.id)
    await setup.conversations.log.record_compacted(gem_id, None, summary=summary, through=through)


async def test_a_reply_is_sent_the_summary_then_everything_from_its_line_on() -> None:
    setup = await _talked("One.", "Two.")
    await _compact(setup, await _said_at(setup, "message 2"))
    setup.llm.then(_said("Three."))

    setup.socket.send(setup.gem.id, "message 3")
    await setup.socket.until("chat.done", 3)

    # The line that gave the time is summed up, so the message is given the time again.
    assert setup.llm.requests[-1]["input"] == [
        summed_up("They talked."),
        {"role": "user", "content": "message 2"},
        {"role": "assistant", "content": "Two."},
        TIME_LINE,
        {"role": "user", "content": "message 3"},
    ]
    # What is stored is not changed, and the history shows it all.
    assert [text for _, text in await setup.shown()][:2] == ["message 1", "One."]
    await _close(setup.socket, setup.linked)


async def _remember(setup: Any, *items: Any) -> None:
    """Add items to the gem's conversation, as a reply would have, and store them."""
    gem_id = ObjectId(setup.gem.id)
    await setup.conversations.log.open(gem_id)
    for item in items:
        setup.conversations.log.remember(gem_id, Added(item, datetime.now(UTC)), "chat")
    await setup.conversations.log.stored(gem_id)


_LOADED = "tracker__find_issues"


async def _found(arguments: str) -> str:
    return "4"


class _Connected:
    """A connection open to every gem, whose one tool is deferred."""

    async def tools_for(self, gem_id: object) -> list[Tool]:
        spec = ToolSpec(_LOADED, "Find issues.", {"type": "object"}, strict=False, deferred=True)
        return [Tool(spec, _found, connection="Tracker")]


async def test_the_requests_after_a_compaction_keep_every_byte() -> None:
    # A deferred tool is loaded by a search in the part summed up, and called after it.
    setup = await _talked("One.")
    setup.chat._connections = _Connected()
    search = {"type": "function_call", "call_id": "s1", "name": SEARCH_TOOL, "arguments": "{}"}
    found = {
        "type": "function_call_output",
        "call_id": "s1",
        "output": f'{{"tools": ["{_LOADED}"]}}',
    }
    await _remember(setup, search, found)
    setup.llm.then(_said("Two."))
    setup.socket.send(setup.gem.id, "message 2")
    await setup.socket.until("chat.done", 2)
    called = {"type": "function_call", "call_id": "c1", "name": _LOADED, "arguments": "{}"}
    await _remember(setup, called, {"type": "function_call_output", "call_id": "c1", "output": "4"})
    await _compact(setup, await _said_at(setup, "message 2"))
    setup.llm.then(_said("Three."), _said("Four."))

    for count in (3, 4):
        setup.socket.send(setup.gem.id, f"message {count}")
        await setup.socket.until("chat.done", count)

    before, after = setup.llm.requests[-2:]
    assert json.dumps(after["input"]).startswith(json.dumps(before["input"])[:-1])
    assert (after["instructions"], after["tools"]) == (before["instructions"], before["tools"])
    # The search is sent after the summary, so the tool it loaded is defined in the request.
    assert [item.get("type") for item in after["input"][1:3]] == [
        "tool_search_call",
        "tool_search_output",
    ]
    assert [tool["name"] for tool in after["input"][2]["tools"]] == [_LOADED]
    # On Anthropic, each request's messages start with all of the one before.
    sent = [
        (await _sent(setup, request["seq"]))["items"]
        for request in [e for e in await _log(setup) if e["kind"] == "request.sent"][-2:]
    ]
    tools = {_LOADED, SEARCH_TOOL}
    first, second = (json.dumps(to_messages(items, tools)) for items in sent)
    assert second.startswith(first[:-1])
    await _close(setup.socket, setup.linked)


async def _sent(setup: Any, seq: int) -> dict[str, Any]:
    service = GemsServiceV1Class(setup.gems, setup.conversations)
    return await service.get_log_sent(str(setup.gem.id), seq)


async def test_what_a_request_was_sent_is_read_back_with_the_compaction_in_force() -> None:
    setup = await _talked("One.", "Two.")
    await _compact(setup, await _said_at(setup, "message 2"))
    setup.llm.then(_said("Three."))
    setup.socket.send(setup.gem.id, "message 3")
    await setup.socket.until("chat.done", 3)

    requests = [entry for entry in await _log(setup) if entry["kind"] == "request.sent"]
    service = GemsServiceV1Class(setup.gems, setup.conversations)
    gem_id = str(setup.gem.id)
    before = await service.get_log_sent(gem_id, requests[1]["seq"])
    after = await service.get_log_sent(gem_id, requests[2]["seq"])

    assert before["exact"] and after["exact"]
    assert after["items"] == setup.llm.requests[2]["input"]
    assert after["items"][0] == summed_up("They talked.")
    # A request before the compaction was sent everything whole.
    assert before["items"] == setup.llm.requests[1]["input"]
    await _close(setup.socket, setup.linked)


async def test_a_reset_ends_the_compaction_in_force() -> None:
    setup = await _talked("One.", "Two.")
    await _compact(setup, await _said_at(setup, "message 2"))
    setup.llm.then(_said("Three."))

    await setup.conversations.reset(str(setup.gem.id))
    setup.socket.send(setup.gem.id, "message 3")
    await setup.socket.until("chat.done", 3)

    assert setup.llm.requests[-1]["input"] == [TIME_LINE, {"role": "user", "content": "message 3"}]
    await _close(setup.socket, setup.linked)


async def test_a_call_starts_from_the_summary_and_what_follows_its_line() -> None:
    world = await _world(_said("One."), _said("Two."), _spoken("Hello."))
    await world.write("message 1")
    await world.write("message 2")
    log = await world.store.page(world.gem.id, before=None, limit=500)
    second = next(
        e.seq for e in log if e.kind == "user.said" and e.data["item"]["content"] == "message 2"
    )
    await world.conversations.log.record_compacted(
        world.gem.id, None, summary="They talked.", through=second
    )

    world.llm.starts_anew()
    call, f, keeping = await world.call()
    await _said_and_heard(f, "hello")
    await _hang_up(call, keeping)

    sent = world.llm.requests[-1]["input"]
    assert sent[:3] == [
        summed_up("They talked."),
        {"role": "user", "content": "message 2"},
        {"role": "assistant", "content": "Two."},
    ]
    assert {"role": "user", "content": "message 1"} not in sent
