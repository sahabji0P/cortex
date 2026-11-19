"""A gem's log, as whole conversations leave it: written here, and spoken further down."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from bson import ObjectId

from app.core.errors import NotFoundError
from app.routers.gems.service import GemsServiceV1Class
from app.routers.link.service import _trimmed
from tests.test_chat import (
    _WEATHER,
    _Chat,
    _chat,
    _close,
    _completed,
    _cut_while_searching,
    _delta,
    _looks,
    _open,
    _said,
    _Searches,
)
from tests.test_one_conversation import (
    _CALLED,
    _DELAY,
    _gem_said,
    _hang_up,
    _let_audio_out,
    _read_until,
    _said_and_heard,
    _spoken,
    _user,
    _World,
    _world,
)


async def _log(setup: _Chat) -> list[dict[str, Any]]:
    """The gem's log, in order, once everything that happened is written."""
    await setup.conversations.log.written()
    return sorted(setup.docdb.collections.get("GemLog", []), key=lambda d: d["seq"])


def _kinds(log: list[dict[str, Any]]) -> list[str]:
    return [entry["kind"] for entry in log]


def _of(log: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [entry.get("data", entry) for entry in log if entry["kind"] == kind]


async def test_a_written_reply_leaves_its_turn_its_request_and_what_is_remembered() -> None:
    setup = await _chat([_delta("Hi "), _delta("there."), _completed()])
    socket, linked = await _open(setup.service())

    socket.send(setup.gem.id, "hello")
    await socket.until("chat.done")
    log = await _log(setup)

    assert _kinds(log) == [
        "turn.opened",
        "user.said",
        "settings.changed",
        "request.sent",
        "request.done",
        "turn.closed",
        "item",
        "item",
    ]
    assert [entry["seq"] for entry in log] == list(range(8))
    # Everything of the turn names it; what is remembered belongs to no turn.
    assert [entry.get("turn") for entry in log] == [1, 1, 1, 1, 1, 1, None, None]
    assert _of(log, "turn.opened") == [{"by": "user", "mode": "chat"}]
    assert _of(log, "user.said") == [{"text": "hello", "mode": "chat"}]
    (settings,) = _of(log, "settings.changed")
    assert settings["model"] == {"name": "6 Luna", "provider": "OpenAI", "via": "api"}
    assert [layer["name"] for layer in settings["instructions"]] == [
        "darbar",
        "gem",
        "model",
        "date",
    ]
    # The layers are the instructions the model was sent, cut where they join.
    assert (
        "".join(layer["text"] for layer in settings["instructions"])
        == (setup.llm.requests[0]["instructions"])
    )
    assert [tool["name"] for tool in settings["tools"]] == [
        tool["name"] for tool in setup.llm.requests[0]["tools"]
    ]
    (sent,) = _of(log, "request.sent")
    assert (sent["kept"], sent["added"]) == (0, setup.llm.requests[0]["input"])
    (done,) = _of(log, "request.done")
    assert (done["outcome"], done["text"], done["calls"]) == ("done", "Hi there.", [])
    assert _of(log, "turn.closed") == [{"outcome": "finished", "kept": None, "error": None}]
    assert [(item["source"], item["item"]["content"]) for item in _of(log, "item")] == [
        ("user", "hello"),
        ("gem", "Hi there."),
    ]
    await _close(socket, linked)


async def test_a_message_whose_reply_left_nothing_is_sent_again_joined_to_the_next() -> None:
    setup = await _chat([SimpleNamespace(type="error", message="boom")], _said("Hello to both."))
    socket, linked = await _open(setup.service())

    socket.send(setup.gem.id, "one")
    await socket.until("chat.error")
    socket.send(setup.gem.id, "two")
    await socket.until("chat.done")
    log = await _log(setup)

    assert _kinds(log) == [
        "turn.opened",
        "user.said",
        "settings.changed",
        "request.sent",
        "request.done",
        "turn.closed",
        "turn.opened",
        "user.said",
        "request.sent",
        "request.done",
        "turn.closed",
        "item",
        "item",
    ]
    # Each message is written down as it arrived, in its own turn.
    assert [said["text"] for said in _of(log, "user.said")] == ["one", "two"]
    failed, answered = _of(log, "request.done")
    assert (failed["outcome"], failed["error"]) == ("failed", "boom")
    assert _of(log, "turn.closed")[0] == {"outcome": "failed", "kept": None, "error": "boom"}
    first, second = _of(log, "request.sent")
    assert first["added"] == [{"role": "user", "content": "one"}]
    # The second request keeps nothing of the first: the user's turn gained a line, so
    # the one item it was sent is not the one sent before.
    assert (second["kept"], second["added"]) == (0, [{"role": "user", "content": "one\ntwo"}])
    assert second["added"] == setup.llm.requests[1]["input"]
    # What is remembered is the two as one message, and the reply to it.
    assert [item["item"]["content"] for item in _of(log, "item")] == ["one\ntwo", "Hello to both."]
    assert answered["text"] == "Hello to both."
    await _close(socket, linked)


async def test_a_search_left_running_at_a_cut_ends_later_and_its_result_is_told() -> None:
    searches = _Searches(_WEATHER)
    setup = await _chat(_looks(), _said("Noon."), _said("It is sunny in Pune."), search=searches)
    socket, linked = await _open(setup.service())

    await _cut_while_searching(setup, socket)
    searches.gates[_WEATHER].set()
    await socket.until("chat.done", 2)
    log = await _log(setup)

    assert _kinds(log) == [
        # The reply that searched, cut by the next message while the search ran.
        "turn.opened",
        "user.said",
        "settings.changed",
        "request.sent",
        "request.done",
        "tool.started",
        "turn.closed",
        "item",
        "item",
        "item",
        "item",
        # The message that cut it, answered while the search still ran.
        "turn.opened",
        "user.said",
        "request.sent",
        "request.done",
        "turn.closed",
        "item",
        "item",
        # The search ends, in nobody's turn of the moment, and is told in a reply.
        "tool.done",
        "item",
        "item",
        "turn.opened",
        "request.sent",
        "request.done",
        "turn.closed",
        "item",
    ]
    assert [opened["by"] for opened in _of(log, "turn.opened")] == ["user", "user", "late_result"]
    (started,) = _of(log, "tool.started")
    assert (started["name"], started["request"]) == ("web_search", 1)
    (ended,) = _of(log, "tool.done")
    assert (ended["outcome"], ended["late"], ended["call_id"]) == ("done", True, started["call_id"])
    # The search belongs to the turn that made it, though it ended two turns later.
    assert [e["turn"] for e in log if e["kind"].startswith("tool.")] == [1, 1]
    cut, _, _ = _of(log, "turn.closed")
    # Cut while the tool ran: the text before the call is whole, so nothing was cut off.
    assert cut == {"outcome": "cut", "kept": None, "error": None}
    assert [item["source"] for item in _of(log, "item")] == [
        "user",
        "gem",
        "gem",
        "placeholder",
        "user",
        "gem",
        "late-result",
        "late-result",
        "gem",
    ]
    # Each request says what it adds to the one before it.
    first, second, third = _of(log, "request.sent")
    assert (first["kept"], second["kept"], third["kept"]) == (0, 1, 5)
    assert [len(sent["added"]) for sent in (first, second, third)] == [1, 4, 3]
    assert third["count"] == len(setup.llm.requests[2]["input"]) == 8
    await _close(socket, linked)


# ---- in a call


async def _call_log(world: _World) -> list[dict[str, Any]]:
    """The gem's log as a client is shown it, in order, once everything is written."""
    await world.conversations.log.written()
    page = await world.store.page(world.gem.id, before=None, limit=1000)
    return [line.shown() for line in page]


def _shown(log: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [entry for entry in log if entry["kind"] == kind]


async def test_a_call_is_written_down_from_its_opening_to_its_close() -> None:
    world = await _world(_spoken("Hello."), _spoken("Not much."), speaks_first=True)

    call, f, keeping = await world.call()
    await _read_until(f.conversation, "assistant.done")
    f.clock.advance(60)
    await _said_and_heard(f, "what's new?")
    await _hang_up(call, keeping)
    log = await _call_log(world)

    assert [entry["kind"] for entry in log] == [
        "call.opened",
        "note.written",
        # The gem speaks first: a line of ours asks it to.
        "turn.opened",
        "note.written",
        "settings.changed",
        "request.sent",
        "request.done",
        # The greeting is settled when the user next speaks, and their turn opens.
        "turn.closed",
        "turn.opened",
        "user.said",
        "request.sent",
        "request.done",
        "item",
        "item",
        "item",
        # The last reply is settled when the call ends.
        "turn.closed",
        "item",
        "item",
        "call.closed",
    ]
    assert [note["reason"] for note in _shown(log, "note.written")] == ["call-line", "greeting"]
    # The line that opens the call belongs to no turn. The greeting's line does.
    assert [note["turn"] for note in _shown(log, "note.written")] == [None, 1]
    opened = _shown(log, "turn.opened")
    assert [(turn["by"], turn["mode"]) for turn in opened] == [("gem", "call"), ("user", "call")]
    assert [said["text"] for said in _shown(log, "user.said")] == ["what's new?"]
    assert {closed["outcome"] for closed in _shown(log, "turn.closed")} == {"finished"}
    assert [(item["source"], item["mode"]) for item in _shown(log, "item")] == [
        ("call-line", "call"),
        ("greeting", "call"),
        ("gem", "call"),
        ("user", "call"),
        ("gem", "call"),
    ]
    first, second = _shown(log, "request.sent")
    assert (first["kept"], first["count"]) == (0, 2)
    assert (second["kept"], second["count"]) == (2, 4)
    assert second["added"] == world.llm.requests[1]["input"][2:]


async def test_a_reply_cut_in_a_call_keeps_what_was_heard_beside_all_that_was_produced() -> None:
    never = asyncio.Event()
    world = await _world([_delta("Once upon a time there was a dragon."), never], _spoken("Sure."))

    call, f, keeping = await world.call()
    f.stt.say("tell me a story")
    await _read_until(f.conversation, "assistant.word")
    await _let_audio_out()
    f.clock.advance(_DELAY + 3.5)  # three and a half words in
    f.stt.turn_start()
    await _read_until(f.conversation, "interrupt")
    f.stt.turn_end("never mind")
    await _read_until(f.conversation, "assistant.done")
    f.clock.advance(60)
    await _hang_up(call, keeping)
    log = await _call_log(world)

    cut, answered = _shown(log, "turn.closed")
    assert (cut["turn"], cut["outcome"], cut["kept"]) == (1, "cut", "Once upon a…")
    assert answered["outcome"] == "finished"
    stopped, done = _shown(log, "request.done")
    # The model was still producing when it was cut: all it produced is kept here, and
    # what it used was never told.
    assert (stopped["turn"], stopped["outcome"]) == (1, "cut")
    assert stopped["text"] == "Once upon a time there was a dragon."
    assert (stopped["usage"], stopped["cost_usd"]) == (None, None)
    assert (done["outcome"], done["text"]) == ("done", "Sure.")
    # A turn is settled the instant the user cuts in, before its request has stopped.
    kinds = [entry["kind"] for entry in log]
    assert kinds.index("turn.closed") < kinds.index("request.done")
    # What is remembered is what was heard.
    assert [item["item"]["content"] for item in _shown(log, "item")] == [
        _CALLED["content"],
        "tell me a story",
        "Once upon a…",
        "never mind",
        "Sure.",
    ]
    # The second request keeps the line and the user's words, and adds what was heard.
    _, second = _shown(log, "request.sent")
    assert (second["kept"], second["added"]) == (
        2,
        [_gem_said("Once upon a…"), _user("never mind")],
    )


async def test_a_call_and_writing_share_one_log_and_one_count_of_turns() -> None:
    world = await _world(_spoken("Hi there."), _said("Sunny."))

    call, f, keeping = await world.call()
    await _said_and_heard(f, "hello")
    await _hang_up(call, keeping)
    await world.write("weather?")
    log = await _call_log(world)

    assert [entry["seq"] for entry in log] == list(range(len(log)))
    opened = _shown(log, "turn.opened")
    assert [(turn["turn"], turn["mode"]) for turn in opened] == [(1, "call"), (2, "chat")]
    assert [sent["request"] for sent in _shown(log, "request.sent")] == [1, 2]
    assert [note["reason"] for note in _shown(log, "note.written")] == ["call-line", "chat-line"]
    # One change at the first request. The second is made with the same model,
    # instructions and tools, so nothing is said to have changed.
    assert len(_shown(log, "settings.changed")) == 1
    kinds = [entry["kind"] for entry in log]
    assert kinds.index("call.closed") < kinds.index("turn.opened", kinds.index("call.closed"))


async def test_a_reply_that_fails_in_a_call_closes_its_turn_as_failed() -> None:
    failed = SimpleNamespace(
        type="response.failed",
        response=SimpleNamespace(error=SimpleNamespace(message="overloaded")),
    )
    world = await _world([failed], _spoken("Hi."))

    call, f, keeping = await world.call()
    f.stt.say("hello")
    await _read_until(f.conversation, "error")
    await _said_and_heard(f, "hello?")
    await _hang_up(call, keeping)
    log = await _call_log(world)

    first, second = _shown(log, "turn.closed")
    assert (first["turn"], first["outcome"], first["error"]) == (1, "failed", "overloaded")
    assert second["outcome"] == "finished"
    stopped, done = _shown(log, "request.done")
    assert (stopped["outcome"], stopped["error"], stopped["usage"]) == (
        "failed",
        "overloaded",
        None,
    )
    assert done["outcome"] == "done"


async def test_a_call_closed_as_it_opens_leaves_no_turn_open_in_the_log() -> None:
    world = await _world(_spoken("Hello."), speaks_first=True)

    # The gem is to speak first, and the call ends before its reply has begun: nothing
    # is waited for in between.
    call = await world.calls.open(str(world.gem.id))
    await call.close()
    log = await _call_log(world)

    assert [entry["kind"] for entry in log] == [
        "call.opened",
        "note.written",
        "turn.opened",
        "note.written",
        "turn.closed",
        "call.closed",
    ]
    (closed,) = _shown(log, "turn.closed")
    assert (closed["turn"], closed["outcome"], closed["kept"]) == (1, "cut", None)
    # Nothing was said, so nothing is remembered.
    assert await world.stored() == []


async def test_a_greeting_cut_before_a_word_of_it_is_heard_keeps_nothing() -> None:
    never = asyncio.Event()
    world = await _world([_delta("Hello there."), never], _spoken("Yes?"), speaks_first=True)

    call, f, keeping = await world.call()
    await _read_until(f.conversation, "assistant.word")
    # The user speaks before the sound device has played any of it.
    f.stt.turn_start()
    await _read_until(f.conversation, "interrupt")
    f.stt.turn_end("wait")
    await _read_until(f.conversation, "assistant.done")
    f.clock.advance(60)
    await _hang_up(call, keeping)
    log = await _call_log(world)

    greeting, _ = _shown(log, "turn.closed")
    # Cut, with nothing delivered: the text kept is empty, not missing.
    assert (greeting["outcome"], greeting["kept"]) == ("cut", "")
    cut, _ = _shown(log, "request.done")
    assert (cut["outcome"], cut["text"]) == ("cut", "Hello there.")


async def test_a_call_that_ends_mid_reply_closes_the_turn_then_the_request_then_the_call() -> None:
    never = asyncio.Event()
    world = await _world([_delta("Once upon a time."), never])

    call, f, keeping = await world.call()
    f.stt.say("tell me a story")
    await _read_until(f.conversation, "assistant.word")
    await _hang_up(call, keeping)
    log = await _call_log(world)

    kinds = [entry["kind"] for entry in log]
    assert kinds[-1] == "call.closed"
    assert kinds.index("turn.closed") < kinds.index("request.done") < kinds.index("call.closed")
    assert _shown(log, "request.done")[0]["outcome"] == "cut"
    # Every turn and every request that opened has closed.
    assert kinds.count("turn.opened") == kinds.count("turn.closed") == 1
    assert kinds.count("request.sent") == kinds.count("request.done") == 1


async def test_a_message_cut_before_the_model_is_asked_leaves_a_turn_with_no_request() -> None:
    gate = asyncio.Event()
    setup = await _chat([_delta("Hi."), gate, _completed()], _said("Hello to all three."))
    socket, linked = await _open(setup.service())

    # The first is being answered when two more come at once: the second never is.
    socket.send(setup.gem.id, "one")
    await socket.until("chat.text")
    for text in ("two", "three"):
        socket.send(setup.gem.id, text)
    await socket.until("chat.done")
    log = await _log(setup)

    turns = {turn: [e["kind"] for e in log if e.get("turn") == turn] for turn in (1, 2, 3)}
    assert turns[2] == ["turn.opened", "user.said", "turn.closed"]
    assert [closed["outcome"] for closed in _of(log, "turn.closed")] == ["cut", "cut", "finished"]
    assert [said["text"] for said in _of(log, "user.said")] == ["one", "two", "three"]
    assert len(_of(log, "request.sent")) == len(_of(log, "request.done")) == 2
    await _close(socket, linked)


# ---- read back over the routes


async def test_the_log_route_gives_the_latest_page_and_then_what_is_before_it() -> None:
    setup = await _chat(_said("Hi."), _said("Hi again."))
    socket, linked = await _open(setup.service())
    for text in ("hello", "hello again"):
        socket.send(setup.gem.id, text)
        await socket.until("chat.done", 1 if text == "hello" else 2)
    gems = GemsServiceV1Class(setup.gems, setup.conversations)
    gem_id = str(setup.gem.id)

    latest = (await gems.get_log(gem_id, before=None, limit=5)).entries
    earlier = (await gems.get_log(gem_id, before=latest[0]["seq"], limit=500)).entries

    whole = [*earlier, *latest]
    assert [entry["seq"] for entry in whole] == list(range(15))
    assert len(latest) == 5
    assert [entry["kind"] for entry in latest] == [
        "request.sent",
        "request.done",
        "turn.closed",
        "item",
        "item",
    ]
    # An entry is its own fields beside the four every entry has.
    opened = whole[0]
    assert opened == {
        "seq": 0,
        "at": opened["at"],
        "kind": "turn.opened",
        "turn": 1,
        "by": "user",
        "mode": "chat",
    }
    item = latest[-1]
    assert (item["place"], item["source"], item["item"]) == (
        3,
        "gem",
        {"role": "assistant", "content": "Hi again."},
    )
    assert await gems.get_log_entry(gem_id, 0) == opened
    await _close(socket, linked)


# ---- watched over the standing socket


def _watching(setup: _Chat) -> Any:
    service = setup.service()
    service.log = setup.conversations.log
    return service


def _entries(socket: Any) -> list[dict[str, Any]]:
    return [frame["entry"] for frame in socket.frames if frame["type"] == "log.entry"]


async def test_a_socket_that_watches_a_gem_is_sent_its_log_as_it_is_written() -> None:
    setup = await _chat(_said("Hi."))
    socket, linked = await _open(_watching(setup))
    gem_id = str(setup.gem.id)

    socket.raw(json.dumps({"type": "log.watch", "gem": gem_id}))
    socket.send(gem_id, "hello")
    await socket.until("chat.done")
    await socket.until("log.entry", 8)

    # The same entries as the route gives, in the same order, each named with its gem.
    gems = GemsServiceV1Class(setup.gems, setup.conversations)
    assert _entries(socket) == (await gems.get_log(gem_id, before=None, limit=100)).entries
    assert {f["gem"] for f in socket.frames if f["type"] == "log.entry"} == {gem_id}
    await _close(socket, linked)


async def test_a_socket_is_sent_no_log_it_did_not_ask_for() -> None:
    setup = await _chat(_said("Hi."), _said("Hi again."), _said("And again."))
    other = await setup.gems.create(name="Gem Two", model="6 Luna")
    socket, linked = await _open(_watching(setup))

    # Not watching yet, then watching another gem, then no longer watching this one.
    socket.send(setup.gem.id, "one")
    await socket.until("chat.done")
    socket.raw(json.dumps({"type": "log.watch", "gem": str(other.id)}))
    socket.send(setup.gem.id, "two")
    await socket.until("chat.done", 2)
    socket.raw(json.dumps({"type": "log.watch", "gem": str(setup.gem.id)}))
    socket.raw(json.dumps({"type": "log.unwatch", "gem": str(setup.gem.id)}))
    socket.send(setup.gem.id, "three")
    await socket.until("chat.done", 3)
    await setup.conversations.log.written()
    await asyncio.sleep(0)

    assert _entries(socket) == []
    await _close(socket, linked)


async def test_unwatching_with_no_gem_named_stops_every_watch() -> None:
    setup = await _chat(_said("Hi."))
    socket, linked = await _open(_watching(setup))

    socket.raw(json.dumps({"type": "log.watch", "gem": str(setup.gem.id)}))
    socket.raw(json.dumps({"type": "log.watch"}))
    socket.raw(json.dumps({"type": "log.unwatch"}))
    socket.send(setup.gem.id, "hello")
    await socket.until("chat.done")
    await setup.conversations.log.written()
    await asyncio.sleep(0)

    assert _entries(socket) == []
    await _close(socket, linked)


def test_a_long_list_is_cut_short_on_the_link_too() -> None:
    items = [{"role": "user", "content": str(n)} for n in range(80)]

    trimmed = _trimmed({"seq": 3, "kind": "request.sent", "kept": 0, "added": items, "count": 80})
    whole = _trimmed({"seq": 4, "kind": "request.sent", "kept": 80, "added": items[:2]})

    assert (len(trimmed["added"]), trimmed["partial"], trimmed["count"]) == (50, True, 80)
    assert trimmed["added"] == items[:50]
    assert "partial" not in whole


async def test_a_socket_that_names_no_gem_watches_every_gem() -> None:
    setup = await _chat(_said("Hi."))
    socket, linked = await _open(_watching(setup))

    socket.raw(json.dumps({"type": "log.watch"}))
    socket.send(setup.gem.id, "hello")
    await socket.until("log.entry", 8)

    assert [entry["kind"] for entry in _entries(socket)][:2] == ["turn.opened", "user.said"]
    await _close(socket, linked)


async def test_a_long_text_is_cut_short_on_the_link_and_whole_on_the_route() -> None:
    long = "word " * 2000
    setup = await _chat(_said("Hi."))
    socket, linked = await _open(_watching(setup))
    gem_id = str(setup.gem.id)

    socket.raw(json.dumps({"type": "log.watch", "gem": gem_id}))
    socket.send(gem_id, long)
    await socket.until("chat.done")
    await socket.until("log.entry", 8)

    said = next(entry for entry in _entries(socket) if entry["kind"] == "user.said")
    assert (len(said["text"]), said["partial"]) == (4000, True)
    # An entry that was not cut says nothing of it.
    assert "partial" not in _entries(socket)[0]
    gems = GemsServiceV1Class(setup.gems, setup.conversations)
    whole = await gems.get_log_entry(gem_id, said["seq"])
    assert whole["text"] == long and "partial" not in whole
    await _close(socket, linked)


async def test_the_log_routes_say_when_there_is_no_such_gem_or_entry() -> None:
    setup = await _chat(_said("Hi."))
    gems = GemsServiceV1Class(setup.gems, setup.conversations)

    with pytest.raises(NotFoundError):
        await gems.get_log(str(ObjectId()), before=None, limit=10)
    with pytest.raises(NotFoundError):
        await gems.get_log("not an id", before=None, limit=10)
    with pytest.raises(NotFoundError):
        await gems.get_log_entry(str(setup.gem.id), 0)
    assert (await gems.get_log(str(setup.gem.id), before=None, limit=10)).entries == []
