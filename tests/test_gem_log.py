"""The gem's log: its order, and what it holds beside the items of the conversation."""

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

from bson import ObjectId

from app.entities.conversation_ent import ConversationEntityClass, Stored
from app.entities.conversation_store import ConversationStoreClass
from app.entities.gem_ent import GemEntityClass
from app.entities.gem_log import LogEntityClass, Recording
from app.entities.history import Added, line, stubs
from app.entities.instructions import Layer
from app.entities.loop import ToolCall
from app.entities.model import ModelCall, ToolSpec, Usage
from app.entities.tools import Tool
from tests.test_chat import _docdb, _stored_items

_NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def _user(text: str) -> Added:
    return Added({"role": "user", "content": text}, _NOW)


async def _conversations() -> tuple[Any, ConversationStoreClass, ConversationEntityClass]:
    docdb, adapter = _docdb()
    store = ConversationStoreClass(adapter)
    return docdb, store, ConversationEntityClass(gems=GemEntityClass(adapter), store=store)


async def test_items_are_numbered_in_the_order_they_are_added() -> None:
    docdb, _, conversations = await _conversations()
    gem_id = ObjectId()

    stored = await conversations.add(gem_id, Stored([]), [_user("one"), _user("two")], "chat")
    await conversations.add(gem_id, stored, [_user("three")], "chat")

    assert [(d["seq"], d["place"]) for d in _stored_items(docdb)] == [(0, 0), (1, 1), (2, 2)]


async def test_each_gem_has_its_own_numbers() -> None:
    docdb, _, conversations = await _conversations()
    one, other = ObjectId(), ObjectId()

    await conversations.add(one, Stored([]), [_user("hello")], "chat")
    await conversations.add(other, Stored([]), [_user("hello")], "chat")

    assert [d["seq"] for d in _stored_items(docdb)] == [0, 0]


async def test_the_numbers_carry_on_after_a_restart() -> None:
    docdb, store, conversations = await _conversations()
    gem_id = ObjectId()
    stored = await conversations.add(gem_id, Stored([]), [_user("one"), _user("two")], "chat")

    # New entities over the same database, as after a restart.
    again = ConversationEntityClass(gems=conversations._gems, store=store)
    await again.add(gem_id, stored, [_user("three")], "chat")

    assert [d["seq"] for d in _stored_items(docdb)] == [0, 1, 2]


async def test_two_who_open_a_gems_log_at_once_do_not_hand_out_a_number_twice() -> None:
    _, store, conversations = await _conversations()
    gem_id = ObjectId()
    await conversations.add(gem_id, Stored([]), [_user("one")], "chat")
    log = LogEntityClass(store)

    await log.open(gem_id)
    first = log.take(gem_id)
    # A second reading of where the log stands does not undo what was counted since.
    await log.open(gem_id)

    assert list(first) == [1]
    assert list(log.take(gem_id, 2)) == [2, 3]


async def test_an_item_is_stored_with_who_wrote_it() -> None:
    docdb, _, conversations = await _conversations()
    late = ToolCall("late_1", "web_search", '{"deferred_result_for": "c1"}', output="Sunny.")

    await conversations.add(
        ObjectId(),
        Stored([]),
        [
            line("The user has called you.", "call-line"),
            _user("hello"),
            Added({"role": "assistant", "content": "Hi."}, _NOW, source="gem"),
            *stubs([late]),
        ],
        "call",
    )

    assert [d["source"] for d in _stored_items(docdb)] == [
        "call-line",
        "user",
        "gem",
        "late-result",
        "late-result",
    ]


# ---- what happened, beside the items


def _log(docdb: Any) -> list[dict[str, Any]]:
    """Everything stored of every gem's log, in the order of its numbers."""
    return sorted(docdb.collections.get("GemLog", []), key=lambda d: d["seq"])


def _kinds(docdb: Any) -> list[str]:
    return [d["kind"] for d in _log(docdb)]


def _tool(name: str = "web_search", connection: str | None = None, **spec: Any) -> Tool:
    async def run(arguments: str) -> str:
        return "done"

    told = ToolSpec(name, spec.pop("description", "Search the web."), {"type": "object"}, **spec)
    return Tool(told, run, connection=connection)


def _described(recording: Recording, *, model: str = "6 Luna", via: str = "api", **parts: Any):
    recording.describe(
        model=cast("Any", model),
        via=lambda: via,
        layers=parts.get("layers", [Layer("darbar", "Hello.\n\n"), Layer("gem", "You are X.\n")]),
        tools=parts.get("tools", [_tool()]),
    )
    return recording


async def _recording(
    mode: str = "chat",
) -> tuple[Any, ConversationEntityClass, ObjectId, Recording]:
    docdb, _, conversations = await _conversations()
    gem_id = ObjectId()
    await conversations.load(gem_id)
    return docdb, conversations, gem_id, conversations.recording(gem_id, cast("Any", mode))


async def test_what_happens_and_what_is_remembered_share_one_order() -> None:
    docdb, conversations, gem_id, recording = await _recording()

    turn = recording.turn("user")
    turn.said("hello")
    request = turn.sent([{"role": "user", "content": "hello"}], False)
    request.first()
    request.done("Hi.", [], Usage(input=10, cache_read=0, cache_write=0, output=2))
    turn.closed(finished=True, kept=None)
    await conversations.add(
        gem_id,
        Stored([]),
        [_user("hello"), Added({"role": "assistant", "content": "Hi."}, _NOW, source="gem")],
        "chat",
    )

    assert _kinds(docdb) == [
        "turn.opened",
        "user.said",
        "request.sent",
        "request.done",
        "turn.closed",
        "item",
        "item",
    ]
    assert [d["seq"] for d in _log(docdb)] == list(range(7))
    # Only the items are the conversation.
    assert [item.place for item in await conversations._store.load(gem_id)] == [0, 1]


async def test_a_request_is_written_down_with_its_tokens_and_what_they_cost() -> None:
    docdb, conversations, _, recording = await _recording()
    turn = _described(recording).turn("user")

    request = turn.sent([{"role": "user", "content": "hello"}], True)
    request.first()
    request.done(
        "Hi.",
        [ModelCall("c1", "web_search", '{"query": "x"}')],
        Usage(input=300, cache_read=3100, cache_write=0, output=85, thinking=20),
    )
    await conversations.log.written()

    sent, done = (d for d in _log(docdb) if d["kind"].startswith("request."))
    assert sent["data"] == {
        "request": 1,
        "attempt": 1,
        "think": True,
        "kept": 0,
        "added": [{"role": "user", "content": "hello"}],
        "count": 1,
    }
    assert sent["turn"] == done["turn"] == 1
    data = done["data"]
    assert data["first_token_at"] is not None
    assert {key: value for key, value in data.items() if key != "first_token_at"} == {
        "request": 1,
        "attempt": 1,
        "outcome": "done",
        "text": "Hi.",
        "calls": [{"call_id": "c1", "name": "web_search", "arguments": '{"query": "x"}'}],
        "usage": {"input": 300, "cache_read": 3100, "cache_write": 0, "output": 85, "thinking": 20},
        "cost_usd": 0.0001035,
        "priced": "2026-11-18",
        "error": None,
    }


async def test_a_request_cut_while_the_model_produced_has_no_usage_and_no_cost() -> None:
    docdb, conversations, _, recording = await _recording()
    turn = _described(recording).turn("user")

    turn.sent([{"role": "user", "content": "hello"}], False).stopped(
        "Once upon", asyncio.CancelledError()
    )
    turn.sent([{"role": "user", "content": "hello"}], False).stopped(
        "", RuntimeError("the model call failed")
    )
    await conversations.log.written()

    cut, failed = (d["data"] for d in _log(docdb) if d["kind"] == "request.done")
    assert (cut["outcome"], cut["text"], cut["usage"], cut["cost_usd"], cut["error"]) == (
        "cut",
        "Once upon",
        None,
        None,
        None,
    )
    assert (failed["outcome"], failed["error"]) == ("failed", "the model call failed")


async def test_a_request_says_what_it_adds_to_the_gems_request_before_it() -> None:
    docdb, conversations, _, recording = await _recording()
    first: list[Any] = [{"role": "user", "content": "one"}]
    grown = [*first, {"role": "assistant", "content": "Hi."}, {"role": "user", "content": "two"}]
    # The user's open turn gained a line: the last item is not the one sent before.
    joined = [*grown[:2], {"role": "user", "content": "two\nthree"}]

    turn = recording.turn("user")
    for items in (first, grown, joined, joined):
        turn.sent(items, False)
    await conversations.log.written()

    sent = [d["data"] for d in _log(docdb) if d["kind"] == "request.sent"]
    assert [(d["kept"], d["added"], d["count"]) for d in sent] == [
        (0, first, 1),
        (1, grown[1:], 3),
        (2, joined[2:], 3),
        (3, [], 3),
    ]


async def test_a_change_of_model_instructions_or_tools_is_written_before_the_request() -> None:
    docdb, conversations, _, recording = await _recording()
    items: list[Any] = [{"role": "user", "content": "hello"}]

    _described(recording).turn("user").sent(items, False)
    # The same again: nothing changed, so nothing is written but the request.
    _described(recording).turn("user").sent(items, False)
    _described(recording, model="Haiku 5.5").turn("user").sent(items, False)
    changed = [Layer("darbar", "Hello.\n\n"), Layer("gem", "You are Y.\n")]
    _described(recording, model="Haiku 5.5", layers=changed).turn("user").sent(items, False)
    await conversations.log.written()

    assert [k for k in _kinds(docdb) if k != "turn.opened"] == [
        "settings.changed",
        "request.sent",
        "request.sent",
        "settings.changed",
        "request.sent",
        "settings.changed",
        "request.sent",
    ]
    whole, model, instructions = (d["data"] for d in _log(docdb) if d["kind"] == "settings.changed")
    assert set(whole) == {"model", "instructions", "tools"}
    assert whole["model"] == {"name": "6 Luna", "provider": "OpenAI", "via": "api"}
    assert whole["tools"] == [
        {
            "name": "web_search",
            "description": "Search the web.",
            "parameters": '{\n  "type": "object"\n}',
            "connection": None,
            "deferred": False,
        }
    ]
    # Only the part that changed, in full.
    assert model == {"model": {"name": "Haiku 5.5", "provider": "Anthropic", "via": "api"}}
    assert instructions == {
        "instructions": [
            {"name": "darbar", "text": "Hello.\n\n"},
            {"name": "gem", "text": "You are Y.\n"},
        ]
    }


async def test_the_way_to_a_provider_is_read_again_for_every_request() -> None:
    docdb, conversations, _, recording = await _recording()
    via = ["api"]
    recording.describe(model="6 Luna", via=lambda: via[0], layers=[], tools=[])
    turn = recording.turn("user")

    turn.sent([], False)
    via[0] = "proxy"
    turn.sent([], False)
    await conversations.log.written()

    changes = [d["data"] for d in _log(docdb) if d["kind"] == "settings.changed"]
    assert changes[1] == {"model": {"name": "6 Luna", "provider": "OpenAI", "via": "proxy"}}


async def test_after_a_restart_nothing_is_said_to_have_changed_and_the_numbers_carry_on() -> None:
    docdb, conversations, gem_id, recording = await _recording()
    _described(recording).turn("user").sent([{"role": "user", "content": "one"}], False)
    await conversations.log.written()

    again = ConversationEntityClass(gems=conversations._gems, store=conversations._store)
    await again.load(gem_id)
    turn = _described(again.recording(gem_id, "chat")).turn("user")
    turn.sent([{"role": "user", "content": "one"}], False)
    await again.log.written()

    assert _kinds(docdb).count("settings.changed") == 1
    assert [d["turn"] for d in _log(docdb) if d["kind"] == "turn.opened"] == [1, 2]
    requests = [d["data"] for d in _log(docdb) if d["kind"] == "request.sent"]
    assert [d["request"] for d in requests] == [1, 2]
    # What the request before was sent is not kept across a restart: it is sent in full.
    assert requests[1]["kept"] == 0


async def test_a_tool_call_is_written_down_when_it_starts_and_when_it_ends() -> None:
    docdb, conversations, _, recording = await _recording()
    turn = recording.turn("user")
    turn.sent([], False)
    release = asyncio.Event()

    async def run() -> str:
        await release.wait()
        return "Sunny."

    slow = ToolCall("c1", "web_search", '{"query": "x"}', tool=_tool(connection="tracker"))
    slow.running = asyncio.create_task(run())
    stopped = ToolCall("c2", "web_search", "{}")
    stopped.running = asyncio.create_task(run())
    turn.tool(slow)
    turn.tool(stopped)
    # Its reply was cut, and it was left running.
    slow.mark = "placeholder"
    stopped.running.cancel()
    release.set()
    await asyncio.gather(slow.running, stopped.running, return_exceptions=True)
    await asyncio.sleep(0)
    await conversations.log.written()

    started = [d["data"] for d in _log(docdb) if d["kind"] == "tool.started"]
    assert started[0] == {
        "request": 1,
        "call_id": "c1",
        "name": "web_search",
        "arguments": '{"query": "x"}',
        "connection": "tracker",
    }
    done = {d["data"]["call_id"]: d["data"] for d in _log(docdb) if d["kind"] == "tool.done"}
    assert done["c1"] == {"call_id": "c1", "outcome": "done", "result": "Sunny.", "late": True}
    assert done["c2"] == {"call_id": "c2", "outcome": "cancelled", "result": None, "late": False}


async def test_a_turn_closes_as_finished_cut_or_failed() -> None:
    docdb, conversations, _, recording = await _recording("call")

    recording.turn("user").closed(finished=True, kept=None)
    recording.turn("gem").closed(finished=False, kept="Once upon…")
    # Cut before any of it got through: nothing is kept, and it was still cut.
    recording.turn("user").closed(finished=False, kept=None)
    failed = recording.turn("late_result")
    failed.failed(RuntimeError("the model call failed"))
    failed.closed(finished=False, kept=None)
    failed.closed(finished=True, kept=None)  # said once
    await conversations.log.written()

    opened = [d["data"] for d in _log(docdb) if d["kind"] == "turn.opened"]
    assert [d["by"] for d in opened] == ["user", "gem", "user", "late_result"]
    assert {d["mode"] for d in opened} == {"call"}
    assert [d["data"] for d in _log(docdb) if d["kind"] == "turn.closed"] == [
        {"outcome": "finished", "kept": None, "error": None},
        {"outcome": "cut", "kept": "Once upon…", "error": None},
        {"outcome": "cut", "kept": None, "error": None},
        {"outcome": "failed", "kept": None, "error": "the model call failed"},
    ]


async def test_whoever_listens_is_told_each_entry_as_it_is_written_down() -> None:
    _, conversations, gem_id, recording = await _recording("call")
    heard: list[tuple[str, dict[str, Any]]] = []

    async def listener(gem: str, entry: dict[str, Any]) -> None:
        heard.append((gem, entry))

    conversations.log.listen(listener)
    recording.call_opened()
    recording.line("call-line", "The user has called you.")
    await conversations.add(gem_id, Stored([]), [_user("hello")], "call")
    conversations.log.ignore(listener)
    recording.call_closed()
    await asyncio.sleep(0)

    assert [gem for gem, _ in heard] == [str(gem_id)] * 3
    opened, note, item = (entry for _, entry in heard)
    assert (opened["seq"], opened["kind"], opened["turn"]) == (0, "call.opened", None)
    assert opened["at"].endswith("Z")
    assert (note["kind"], note["reason"], note["text"]) == (
        "note.written",
        "call-line",
        "The user has called you.",
    )
    assert (item["kind"], item["place"], item["source"], item["mode"]) == (
        "item",
        0,
        "user",
        "call",
    )
    assert item["item"] == {"role": "user", "content": "hello"}


async def test_a_log_that_cannot_be_written_fails_nothing() -> None:
    docdb, conversations, gem_id, recording = await _recording()
    docdb.breaks_after = 0

    turn = recording.turn("user")
    turn.said("hello")
    await conversations.log.written()
    docdb.breaks_after = None
    await conversations.add(gem_id, Stored([]), [_user("hello")], "chat")

    # What happened was lost. The item is stored, after it in the order.
    assert [(d["kind"], d["seq"]) for d in _log(docdb)] == [("item", 2)]


async def test_nothing_is_written_of_a_gem_that_is_gone() -> None:
    docdb, conversations, gem_id, recording = await _recording()
    recording.turn("user")
    await conversations.log.written()

    assert await conversations.gone(gem_id)
    recording.turn("user").said("hello")
    await conversations.log.written()

    assert _log(docdb) == []


async def test_a_conversation_made_with_no_log_writes_nothing_down() -> None:
    recording = Recording()

    turn = recording.turn("user")
    turn.said("hello")
    request = turn.sent([{"role": "user", "content": "hello"}], False)
    request.done("Hi.", [], None)
    turn.closed(finished=True, kept=None)

    assert (turn.number, request.number) == (None, None)


async def test_the_log_is_read_back_in_pages_newest_first() -> None:
    _, conversations, gem_id, recording = await _recording()
    for _ in range(3):
        recording.turn("user").closed(finished=True, kept=None)
    await conversations.log.written()
    store = conversations._store

    latest = await store.page(gem_id, before=None, limit=4)
    earlier = await store.page(gem_id, before=latest[0].seq, limit=4)

    assert [line.seq for line in latest] == [2, 3, 4, 5]
    assert [line.seq for line in earlier] == [0, 1]
    shown = latest[-1].shown()
    assert shown == {
        "seq": 5,
        "at": shown["at"],
        "kind": "turn.closed",
        "turn": 3,
        "outcome": "finished",
        "kept": None,
        "error": None,
    }
    one = await store.one(gem_id, 4)
    assert one is not None and one.shown()["by"] == "user"
    assert await store.one(gem_id, 99) is None
