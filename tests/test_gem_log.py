"""The gem's log: its order, what a gem remembers in it, and what else it holds."""

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from bson import ObjectId

from app.entities.conversation_ent import ConversationEntityClass, Stored
from app.entities.conversation_store import REMEMBERED, ConversationStoreClass
from app.entities.gem_ent import GemEntityClass
from app.entities.gem_log import LogEntityClass, Recording
from app.entities.history import Added, line, said, stubs
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

    assert [(d["seq"], d["kind"]) for d in _stored_items(docdb)] == [
        (0, "user.said"),
        (1, "user.said"),
        (2, "user.said"),
    ]


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


async def test_what_is_remembered_is_stored_as_the_kind_it_is_with_who_wrote_it() -> None:
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

    assert [(d["kind"], d["data"]["source"]) for d in _stored_items(docdb)] == [
        ("note.written", "call-line"),
        ("user.said", "user"),
        ("gem.said", "gem"),
        ("tool.called", "background-result"),
        ("tool.result", "background-result"),
    ]
    # A stub and its result say which call's result they carry, beside the item.
    assert [d["data"].get("delivers") for d in _stored_items(docdb)] == [
        None,
        None,
        None,
        "c1",
        "c1",
    ]
    # Every entry has one shape: the item is in what is known of it, as the model is
    # sent it, and a field with nothing to say is left out.
    said = _stored_items(docdb)[1]
    assert set(said) == {"_id", "gem_id", "kind", "seq", "at", "turn", "data"}
    assert said["data"] == {
        "item": {"role": "user", "content": "hello"},
        "mode": "call",
        "source": "user",
    }


# ---- what happened, beside what is remembered


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

    # As a conversation does it: each thing is handed over the moment it is final.
    turn = recording.turn("user")
    recording.remember(_user("hello"))
    request = turn.sent([{"role": "user", "content": "hello"}], False)
    request.first()
    request.done("Hi.", [], Usage(input=10, cache_read=0, cache_write=0, output=2))
    recording.remember(Added({"role": "assistant", "content": "Hi."}, _NOW, source="gem"))
    turn.closed(finished=True, kept=None)
    await conversations.stored(gem_id)

    assert _kinds(docdb) == [
        "turn.opened",
        "user.said",
        "request.sent",
        "request.done",
        "gem.said",
        "turn.closed",
    ]
    assert [d["seq"] for d in _log(docdb)] == list(range(6))
    # Only the entries of the kinds a gem remembers are its conversation.
    assert [entry.kind for entry in await conversations._store.load(gem_id)] == [
        "user.said",
        "gem.said",
    ]


async def test_a_request_is_written_down_with_its_tokens_and_what_they_cost() -> None:
    docdb, conversations, _, recording = await _recording()
    turn = _described(recording).turn("user")

    recording.remember(_user("hello"))
    request = turn.sent([{"role": "user", "content": "hello"}], True)
    request.first()
    request.done(
        "Hi.",
        [ModelCall("c1", "web_search", '{"query": "x"}')],
        Usage(input=300, cache_read=3100, cache_write=0, output=85, thinking=20),
    )
    await conversations.log.written()

    sent, done = (d for d in _log(docdb) if d["kind"].startswith("request."))
    # A request holds none of what it was sent: only how many items that was.
    assert sent["data"] == {"request": 1, "attempt": 1, "think": True, "count": 1}
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
        "priced": "2026-11-20",
        "error": None,
    }


async def test_a_request_cut_while_the_model_produced_has_no_usage_and_no_cost() -> None:
    docdb, conversations, _, recording = await _recording()
    turn = _described(recording).turn("user")

    recording.remember(_user("hello"))
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


async def test_a_request_was_sent_what_the_gem_remembered_before_it() -> None:
    docdb, conversations, gem_id, recording = await _recording()
    one: list[Any] = [{"role": "user", "content": "one"}]
    three = [*one, {"role": "assistant", "content": "Hi."}, {"role": "user", "content": "two"}]

    turn = recording.turn("user")
    recording.remember(_user("one"))
    turn.sent(one, False)
    recording.remember(Added({"role": "assistant", "content": "Hi."}, _NOW, source="gem"))
    recording.remember(_user("two"))
    turn.sent(three, False)
    # The same again, as when a reply was cut before it left anything.
    turn.sent(three, False)
    await conversations.stored(gem_id)

    sent = [d for d in _log(docdb) if d["kind"] == "request.sent"]
    assert [d["data"]["count"] for d in sent] == [1, 3, 3]
    # Nothing of it is written a second time: what a request was sent is every
    # remembered entry before it, in order.
    for request, items in zip(sent, (one, three, three), strict=True):
        before = [d for d in _log(docdb) if d["kind"] in REMEMBERED and d["seq"] < request["seq"]]
        assert [d["data"]["item"] for d in before] == items
    assert all(set(d["data"]) == {"request", "attempt", "think", "count"} for d in sent)


async def test_a_change_of_model_instructions_or_tools_is_written_before_the_request() -> None:
    docdb, conversations, _, recording = await _recording()
    items: list[Any] = [{"role": "user", "content": "hello"}]
    recording.remember(_user("hello"))

    _described(recording).turn("user").sent(items, False)
    # The same again: nothing changed, so nothing is written but the request.
    _described(recording).turn("user").sent(items, False)
    _described(recording, model="Haiku 5.5").turn("user").sent(items, False)
    changed = [Layer("darbar", "Hello.\n\n"), Layer("gem", "You are Y.\n")]
    _described(recording, model="Haiku 5.5", layers=changed).turn("user").sent(items, False)
    await conversations.log.written()

    assert [k for k in _kinds(docdb) if k not in ("turn.opened", "user.said")] == [
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
    recording.remember(_user("one"))
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
    # Its reply was cut while it ran: it carries on in the background.
    slow.mark = "placeholder"
    turn.backgrounded(slow)
    stopped.running.cancel()
    release.set()
    await asyncio.gather(slow.running, stopped.running, return_exceptions=True)
    await asyncio.sleep(0)
    await conversations.log.written()

    started = [d["data"] for d in _log(docdb) if d["kind"] == "tool.started"]
    # Its arguments are not written again: they are in the request that made the call.
    assert started[0] == {
        "request": 1,
        "call_id": "c1",
        "name": "web_search",
        "connection": "tracker",
    }
    assert [d["data"] for d in _log(docdb) if d["kind"] == "tool.backgrounded"] == [
        {"call_id": "c1"}
    ]
    done = {d["data"]["call_id"]: d["data"] for d in _log(docdb) if d["kind"] == "tool.done"}
    seconds = {call_id: data.pop("seconds") for call_id, data in done.items()}
    assert all(isinstance(took, float) and took >= 0 for took in seconds.values())
    # It ended in the background, where nothing remembers its result yet: it is here.
    assert done["c1"] == {
        "call_id": "c1",
        "outcome": "done",
        "background": True,
        "result": "Sunny.",
    }
    assert done["c2"] == {"call_id": "c2", "outcome": "cancelled", "background": False}
    # Each entry of a call names the turn that made it.
    assert {d["turn"] for d in _log(docdb) if d["kind"].startswith("tool.")} == {1}


async def test_a_tool_that_failed_within_its_reply_ends_as_failed() -> None:
    docdb, conversations, _, recording = await _recording()
    turn = recording.turn("user")

    async def run() -> str:
        return "web_search failed: no network"

    call = ToolCall("c1", "web_search", "{}")
    call.running = asyncio.create_task(run())
    turn.tool(call)
    # The loop notes on the call that its tool failed, beside the text.
    call.failed = True
    await call.running
    await asyncio.sleep(0)
    await conversations.log.written()

    (done,) = (d["data"] for d in _log(docdb) if d["kind"] == "tool.done")
    # It ended within its reply: what the model was told is in the entry that remembers
    # the result, and is not written here a second time.
    assert {key: value for key, value in done.items() if key != "seconds"} == {
        "call_id": "c1",
        "outcome": "failed",
        "background": False,
    }


async def test_a_request_whose_cost_cannot_be_worked_out_is_still_written_down() -> None:
    docdb, conversations, _, recording = await _recording()
    turn = _described(recording).turn("user")

    # A count that is no number: the cost is unknown, and the request's end is kept.
    odd = Usage(input=cast("Any", "many"), cache_read=0, cache_write=0, output=3)
    turn.sent([], False).done("Hi.", [], odd)
    await conversations.log.written()

    (done,) = (d["data"] for d in _log(docdb) if d["kind"] == "request.done")
    assert (done["outcome"], done["text"], done["cost_usd"]) == ("done", "Hi.", None)


async def test_a_time_is_shown_the_same_when_it_is_told_and_when_it_is_read_back() -> None:
    _, conversations, gem_id, recording = await _recording()
    heard: list[dict[str, Any]] = []

    async def listener(gem: str, entry: dict[str, Any]) -> None:
        heard.append(entry)

    conversations.log.listen(listener)
    recording.turn("user")
    await conversations.add(gem_id, Stored([]), [_user("hello")], "chat")
    await asyncio.sleep(0)

    read = [line.shown() for line in await conversations._store.page(gem_id, before=None, limit=9)]
    assert [entry["at"] for entry in heard] == [entry["at"] for entry in read]
    # To the millisecond, in UTC.
    assert all(len(entry["at"]) == len("2026-11-19T12:00:00.000Z") for entry in read)


async def test_a_turn_closes_as_finished_cut_or_failed() -> None:
    docdb, conversations, _, recording = await _recording("call")

    recording.turn("user").closed(finished=True, kept=None)
    recording.turn("gem").closed(finished=False, kept="Once upon…")
    # Cut before any of it got through: nothing is kept, and it was still cut.
    recording.turn("user").closed(finished=False, kept=None)
    failed = recording.turn("background_result")
    failed.failed(RuntimeError("the model call failed"))
    failed.closed(finished=False, kept=None)
    failed.closed(finished=True, kept=None)  # said once
    await conversations.log.written()

    opened = [d["data"] for d in _log(docdb) if d["kind"] == "turn.opened"]
    assert [d["by"] for d in opened] == ["user", "gem", "user", "background_result"]
    assert {d["mode"] for d in opened} == {"call"}
    assert [d["data"] for d in _log(docdb) if d["kind"] == "turn.closed"] == [
        {"outcome": "finished", "error": None},
        # What was kept of a cut reply is in the entries that remember it, not here.
        {"outcome": "cut", "error": None},
        {"outcome": "cut", "error": None},
        {"outcome": "failed", "error": "the model call failed"},
    ]


async def test_whoever_listens_is_told_each_entry_as_it_is_written_down() -> None:
    _, conversations, gem_id, recording = await _recording("call")
    heard: list[tuple[str, dict[str, Any]]] = []

    async def listener(gem: str, entry: dict[str, Any]) -> None:
        heard.append((gem, entry))

    conversations.log.listen(listener)
    recording.call_opened()
    recording.remember(line("The user has called you.", "call-line"))
    recording.remember(_user("hello"))
    conversations.log.ignore(listener)
    recording.call_closed()
    await asyncio.sleep(0)

    # Each is told the moment it is numbered, in order, whatever kind it is.
    assert [gem for gem, _ in heard] == [str(gem_id)] * 3
    opened, note, said = (entry for _, entry in heard)
    assert [entry["seq"] for _, entry in heard] == [0, 1, 2]
    assert (opened["kind"], opened["turn"]) == ("call.opened", None)
    assert opened["at"].endswith("Z")
    assert (note["kind"], note["source"], note["mode"]) == ("note.written", "call-line", "call")
    assert note["item"] == {"role": "developer", "content": "The user has called you."}
    assert (said["kind"], said["source"], said["mode"]) == ("user.said", "user", "call")
    assert said["item"] == {"role": "user", "content": "hello"}


async def test_a_log_that_cannot_be_written_fails_nothing() -> None:
    docdb, conversations, gem_id, recording = await _recording()
    docdb.breaks_after = 0

    turn = recording.turn("user")
    turn.sent([], False)
    await conversations.log.written()
    docdb.breaks_after = None
    await conversations.add(gem_id, Stored([]), [_user("hello")], "chat")

    # What happened was lost, and nothing failed for it. What is remembered is stored,
    # after it in the order.
    assert [(d["kind"], d["seq"]) for d in _log(docdb)] == [("user.said", 2)]


async def test_what_is_remembered_and_could_not_be_stored_is_tried_again_in_order() -> None:
    docdb, conversations, gem_id, recording = await _recording()
    docdb.breaks_after = 0

    recording.remember(_user("one"))
    await conversations.log.written()
    # Whoever waits for it to be stored is told it is not.
    with pytest.raises(ConnectionError):
        await conversations.log.stored(gem_id)
    assert _log(docdb) == []

    # The database is back. What is added now is not stored ahead of what was held.
    docdb.breaks_after = None
    recording.remember(_user("two"))
    await conversations.log.stored(gem_id)

    assert [(d["seq"], d["data"]["item"]["content"]) for d in _log(docdb)] == [
        (0, "one"),
        (1, "two"),
    ]


async def test_what_was_given_up_is_never_stored_later() -> None:
    docdb, conversations, gem_id, recording = await _recording()
    docdb.breaks_after = 0

    recording.remember(_user("one"))
    # Whoever holds the conversation fails for it, and does not try again.
    with pytest.raises(ConnectionError):
        await conversations.stored(gem_id)
    docdb.breaks_after = None
    recording.remember(_user("two"))
    await conversations.stored(gem_id)

    # What comes next started from what was stored, so "one" must not turn up after it.
    assert [d["data"]["item"]["content"] for d in _log(docdb)] == ["two"]


async def test_what_is_given_up_while_its_write_is_on_its_way_is_not_stored_when_that_fails() -> (
    None
):
    docdb, conversations, gem_id, recording = await _recording()
    docdb.slow = asyncio.Event()
    docdb.breaks_after = 0

    recording.remember(_user("given up"))
    waiting = asyncio.create_task(conversations.stored(gem_id))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # Whoever waited is stopped while the write is on its way, as when the user hangs
    # up while a call is opening. Only then does the write fail.
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    docdb.slow.set()
    await conversations.log.written(gem_id)
    docdb.slow, docdb.breaks_after = None, None

    # What comes next starts from what is stored, so what was given up stays out.
    assert (await conversations.load(gem_id)).items == []
    recording.remember(_user("next"))
    await conversations.stored(gem_id)
    assert [d["data"]["item"]["content"] for d in _log(docdb)] == ["next"]


async def test_a_writer_that_is_stopped_mid_write_loses_nothing_that_is_remembered() -> None:
    docdb, conversations, gem_id, recording = await _recording()
    docdb.slow = asyncio.Event()

    recording.remember(_user("held"))
    await asyncio.sleep(0)
    writer = conversations.log._gems[gem_id].writing
    assert writer is not None
    writer.cancel()
    await asyncio.gather(writer, return_exceptions=True)
    docdb.slow = None

    # Nobody was told it is stored: whoever waits next has it written.
    await conversations.stored(gem_id)
    assert [d["data"]["item"]["content"] for d in _log(docdb)] == ["held"]


async def test_a_change_that_could_not_be_written_down_is_said_again_in_full() -> None:
    docdb, conversations, gem_id, recording = await _recording()
    _described(recording)

    docdb.breaks_after = 0
    recording.request(None, [], False)
    await conversations.log.written(gem_id)
    docdb.breaks_after = None
    recording.request(None, [], False)
    await conversations.log.written(gem_id)

    (changed,) = (d["data"] for d in _log(docdb) if d["kind"] == "settings.changed")
    assert set(changed) == {"model", "instructions", "tools"}


async def test_a_turn_whose_opening_was_lost_does_not_give_its_number_away_after_a_restart() -> (
    None
):
    docdb, conversations, gem_id, recording = await _recording()

    # The turn's opening and its request are lost. What the user said in it is stored.
    docdb.breaks_after = 0
    turn = recording.turn("user")
    request = turn.sent([], False)
    await conversations.log.written(gem_id)
    docdb.breaks_after = None
    recording.remember(said("hello", turn=turn.number))
    request.done("Hi.", [], None)
    await conversations.stored(gem_id)

    again = ConversationEntityClass(gems=conversations._gems, store=conversations._store)
    await again.load(gem_id)
    later = again.recording(gem_id, "chat").turn("user")
    assert (turn.number, later.number) == (1, 2)
    assert later.sent([{"role": "user", "content": "hello"}], False).number == 2


async def test_a_write_that_stops_part_way_leaves_no_call_without_its_result() -> None:
    docdb, conversations, gem_id, recording = await _recording()
    call = ToolCall("c1", "web_search", "{}", output="Sunny.")
    # The write stops after its first entry.
    docdb.breaks_after = 1

    for added in stubs([call], "gem"):
        recording.remember(added)
    with pytest.raises(ConnectionError):
        await conversations.log.stored(gem_id)

    # What it did put in was taken out again.
    assert _log(docdb) == []
    docdb.breaks_after = None
    await conversations.log.stored(gem_id)
    assert _kinds(docdb) == ["tool.called", "tool.result"]


async def test_nothing_is_written_of_a_gem_that_is_gone() -> None:
    docdb, conversations, gem_id, recording = await _recording()
    recording.turn("user")
    await conversations.log.written()

    assert await conversations.gone(gem_id)
    recording.turn("user")
    recording.remember(_user("hello"))
    await conversations.stored(gem_id)

    assert _log(docdb) == []


async def test_deleting_a_gem_ends_its_log_for_whatever_was_still_running() -> None:
    docdb, _, conversations = await _conversations()
    gems = conversations._gems
    gem = await gems.create(name="Gem One")
    await conversations.load(gem.id)
    recording = conversations.recording(gem.id, "chat")
    turn = recording.turn("user")
    await conversations.log.written()

    await gems.delete(str(gem.id))
    # A tool call that outlived the gem ends now, and a reply adds what it was writing.
    turn.sent([], False)
    recording.remember(_user("too late"))
    await conversations.stored(gem.id)

    assert _log(docdb) == []


async def test_a_conversation_made_with_no_log_writes_nothing_down() -> None:
    recording = Recording()

    turn = recording.turn("user")
    recording.remember(_user("hello"))
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
        "error": None,
    }
    one = await store.one(gem_id, 4)
    assert one is not None and one.shown()["by"] == "user"
    assert await store.one(gem_id, 99) is None
