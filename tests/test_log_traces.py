"""A gem's log, as whole conversations leave it: written here, and spoken further down."""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from bson import ObjectId

from app.core.errors import NotFoundError
from app.entities.conversation_store import REMEMBERED
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
from tests.test_voice_conversation import _Search, _search_call, _starts
from tests.time_lines import TIME_LINE, set_clock


async def _log(setup: _Chat) -> list[dict[str, Any]]:
    """The gem's log, in order, once everything that happened is written."""
    await setup.conversations.log.written()
    return sorted(setup.docdb.collections.get("GemLog", []), key=lambda d: d["seq"])


def _kinds(log: list[dict[str, Any]]) -> list[str]:
    return [entry["kind"] for entry in log]


def _of(log: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [entry["data"] for entry in log if entry["kind"] == kind]


def _remembered(log: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the gem remembers, of a log as it is stored: those entries' own fields."""
    return [entry["data"] for entry in log if entry["kind"] in REMEMBERED]


def _sent_before(log: list[dict[str, Any]], request: int) -> list[Any]:
    """The items remembered before the log's request of that number, in order."""
    (seq,) = (
        e["seq"] for e in log if e["kind"] == "request.sent" and e["data"]["request"] == request
    )
    return [e["data"]["item"] for e in log if e["kind"] in REMEMBERED and e["seq"] < seq]


async def _sent_is_what_the_model_was_given(
    conversations: Any, gem_id: ObjectId, requests: list[dict[str, Any]]
) -> None:
    """Every request in the gem's log was sent exactly what its model was given, in order.

    What the route says a request was sent is read from the log alone, so this holds
    only while everything a model is sent is remembered before the request is.
    """
    await conversations.log.written()
    log = await conversations._store.page(gem_id, before=None, limit=10_000)
    sent = [entry.seq for entry in log if entry.kind == "request.sent"]
    assert len(sent) == len(requests)
    for seq, request in zip(sent, requests, strict=True):
        told = await conversations.log_sent(str(gem_id), seq)
        assert told["items"] == request["input"]
        assert told["exact"]


async def test_a_written_reply_leaves_its_turn_its_request_and_what_is_remembered() -> None:
    setup = await _chat([_delta("Hi "), _delta("there."), _completed()])
    socket, linked = await _open(setup.service())

    socket.send(setup.gem.id, "hello")
    await socket.until("chat.done")
    log = await _log(setup)

    # Each thing once, as the kind it is, in the order it happened: what the requests
    # are made with first, as a model is sent it first, then the line that gives the
    # time, then the message, remembered when it is sent, before the request that
    # carries it, and the reply when it is settled.
    assert _kinds(log) == [
        "settings.changed",
        "note.written",
        "turn.opened",
        "user.said",
        "request.sent",
        "request.done",
        "gem.said",
        "turn.closed",
    ]
    assert [entry["seq"] for entry in log] == list(range(8))
    # The change and the line that gives the time belong to no turn: they hold from
    # there on. Everything of the turn names it, what is remembered of it included.
    assert [entry.get("turn") for entry in log] == [None, None] + [1] * 6
    (time,) = _of(log, "note.written")
    assert (time["item"], time["mode"], time["source"]) == (TIME_LINE, "chat", "time-line")
    assert _of(log, "turn.opened") == [{"by": "user", "mode": "chat"}]
    assert _of(log, "user.said") == [
        {"item": {"role": "user", "content": "hello"}, "mode": "chat", "source": "user"}
    ]
    (settings,) = _of(log, "settings.changed")
    assert settings["model"] == {"name": "6 Luna", "provider": "OpenAI", "via": "api"}
    assert [layer["name"] for layer in settings["instructions"]] == [
        "darbar",
        "gem",
        "model",
    ]
    # The layers are the instructions the model was sent, cut where they join.
    assert (
        "".join(layer["text"] for layer in settings["instructions"])
        == (setup.llm.requests[0]["instructions"])
    )
    assert [tool["name"] for tool in settings["tools"]] == [
        tool["name"] for tool in setup.llm.requests[0]["tools"]
    ]
    # The request holds none of what it was sent: that is what was remembered before it.
    assert _of(log, "request.sent") == [{"request": 1, "attempt": 1, "think": False, "count": 2}]
    assert _sent_before(log, 1) == setup.llm.requests[0]["input"]
    (done,) = _of(log, "request.done")
    assert (done["outcome"], done["text"], done["calls"]) == ("done", "Hi there.", [])
    assert _of(log, "turn.closed") == [{"outcome": "finished", "error": None}]
    assert _of(log, "gem.said") == [
        {"item": {"role": "assistant", "content": "Hi there."}, "mode": "chat", "source": "gem"}
    ]
    await _close(socket, linked)


async def test_a_message_whose_reply_left_nothing_is_sent_again_with_the_next_after_it() -> None:
    setup = await _chat([SimpleNamespace(type="error", message="boom")], _said("Hello to both."))
    socket, linked = await _open(setup.service())

    socket.send(setup.gem.id, "one")
    await socket.until("chat.error")
    socket.send(setup.gem.id, "two")
    await socket.until("chat.done")
    log = await _log(setup)

    assert _kinds(log) == [
        "settings.changed",
        "note.written",
        "turn.opened",
        "user.said",
        "request.sent",
        "request.done",
        "turn.closed",
        "turn.opened",
        "user.said",
        "request.sent",
        "request.done",
        "gem.said",
        "turn.closed",
    ]
    # Each message is remembered as it was sent, in its own turn, though the reply to
    # the first failed.
    assert [said["item"]["content"] for said in _of(log, "user.said")] == ["one", "two"]
    failed, answered = _of(log, "request.done")
    assert (failed["outcome"], failed["error"]) == ("failed", "boom")
    assert _of(log, "turn.closed")[0] == {"outcome": "failed", "error": "boom"}
    # The second request was sent the whole of the first, and the second message.
    assert [sent["count"] for sent in _of(log, "request.sent")] == [2, 3]
    assert _sent_before(log, 1) == [TIME_LINE, {"role": "user", "content": "one"}]
    assert _sent_before(log, 2) == setup.llm.requests[1]["input"]
    # What is remembered is the time, given once, each message as it was sent, and the
    # reply to both.
    assert [item["item"] for item in _remembered(log)] == [
        TIME_LINE,
        {"role": "user", "content": "one"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "Hello to both."},
    ]
    assert [e["turn"] for e in log if e["kind"] in REMEMBERED] == [None, 1, 2, 2]
    assert answered["text"] == "Hello to both."
    await _close(socket, linked)


async def test_a_second_reply_made_with_the_same_settings_says_nothing_of_them() -> None:
    setup = await _chat(_said("Hi."), _said("Hi again."))
    socket, linked = await _open(setup.service())

    for count, text in enumerate(("hello", "hello again"), start=1):
        socket.send(setup.gem.id, text)
        await socket.until("chat.done", count)
    log = await _log(setup)

    # The second message comes seconds after the first: no line gives the time again.
    assert _kinds(log)[8:] == [
        "turn.opened",
        "user.said",
        "request.sent",
        "request.done",
        "gem.said",
        "turn.closed",
    ]
    assert len(_of(log, "settings.changed")) == 1
    await _close(socket, linked)


async def test_a_message_the_next_day_keeps_the_start_of_the_request_and_no_change_of_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    move = set_clock(monkeypatch, datetime(2026, 11, 20, 22, 0, tzinfo=UTC))
    setup = await _chat(_said("Hi."), _said("Hi again."))
    socket, linked = await _open(setup.service())
    socket.send(setup.gem.id, "hello")
    await socket.until("chat.done")

    move(datetime(2026, 11, 21, 9, 0, tzinfo=UTC))
    socket.send(setup.gem.id, "hello again")
    await socket.until("chat.done", 2)

    # The start of the request stays the same from one day to the next: the same
    # instructions and tools, and the whole of the first request, with nothing in it
    # long enough to be expired. The cache itself is gone by then: a cold start.
    first, second = setup.llm.requests
    assert second["instructions"] == first["instructions"]
    assert second["tools"] == first["tools"]
    assert second["input"][: len(first["input"])] == first["input"]
    # The new day is told by a line of ours, before the message.
    assert first["input"][0]["content"].startswith("It is Friday, 20 November 2026, 10:00 pm")
    assert second["input"][len(first["input"]) :] == [
        {"role": "assistant", "content": "Hi."},
        {
            "role": "developer",
            "content": (
                "It is Saturday, 21 November 2026, 9:00 am UTC. The user's time zone is not known."
            ),
        },
        {"role": "user", "content": "hello again"},
    ]
    # A new day is no change of settings.
    log = await _log(setup)
    assert len(_of(log, "settings.changed")) == 1
    assert [cold["reason"] for cold in _of(log, "cache.cold")] == ["quiet"]
    lines = [entry for entry in _of(log, "note.written") if entry["source"] == "time-line"]
    assert len(lines) == 2
    await _close(socket, linked)


async def test_an_edited_gem_says_its_new_instructions_before_the_next_turn_opens() -> None:
    setup = await _chat(_said("Hi."), _said("Hi again."))
    socket, linked = await _open(setup.service())
    socket.send(setup.gem.id, "hello")
    await socket.until("chat.done")

    await setup.gems.update(str(setup.gem.id), {"instructions": "You are terse."})
    socket.send(setup.gem.id, "hello again")
    await socket.until("chat.done", 2)
    log = await _log(setup)

    first, edited = (e for e in log if e["kind"] == "settings.changed")
    _, second_turn = (e for e in log if e["kind"] == "turn.opened")
    [cold] = (e for e in log if e["kind"] == "cache.cold")
    # Only the instructions changed, and they are said before the turn they are used in.
    # The change starts the conversation cold, and that is said between the two.
    assert set(edited["data"]) == {"instructions"}
    assert edited["turn"] is None
    assert edited["seq"] + 1 == cold["seq"] == second_turn["seq"] - 1
    assert cold["data"]["reason"] == "settings"
    assert edited["data"]["instructions"] != first["data"]["instructions"]
    assert "You are terse." in "".join(layer["text"] for layer in edited["data"]["instructions"])
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
        "settings.changed",
        "note.written",
        "turn.opened",
        "user.said",
        "request.sent",
        "request.done",
        "tool.started",
        # Settled at the cut. The search still runs, so it carries on in the background,
        # and what is remembered is what the gem said, its call, and the placeholder.
        "tool.backgrounded",
        "gem.said",
        "tool.called",
        "tool.result",
        "turn.closed",
        # The message that cut it, answered while the search still ran.
        "turn.opened",
        "user.said",
        "request.sent",
        "request.done",
        "gem.said",
        "turn.closed",
        # The search ends, in nobody's turn of the moment. Its result is added as a stub
        # call with the result, and told in a reply.
        "tool.done",
        "tool.called",
        "tool.result",
        "turn.opened",
        "request.sent",
        "request.done",
        "gem.said",
        "turn.closed",
    ]
    assert [opened["by"] for opened in _of(log, "turn.opened")] == [
        "user",
        "user",
        "background_result",
    ]
    # The whole of the search can be read from the log by its call_id: it started, went
    # to the background, ended, and was delivered.
    (started,) = _of(log, "tool.started")
    assert (started["name"], started["request"]) == ("web_search", 1)
    call_id = started["call_id"]
    assert _of(log, "tool.backgrounded") == [{"call_id": call_id}]
    (ended,) = _of(log, "tool.done")
    assert (ended["outcome"], ended["background"], ended["call_id"]) == ("done", True, call_id)
    assert ended["seconds"] >= 0
    delivered = [e for e in _remembered(log) if e.get("delivers")]
    assert [e["delivers"] for e in delivered] == [call_id, call_id]
    # It ended in the background, so its result is written where it ended, and again
    # where the gem was given it: the one thing the log holds twice.
    assert delivered[1]["item"]["output"] == ended["result"]
    # The search belongs to the turn that made it, though it ended two turns later.
    lifecycle = ("tool.started", "tool.backgrounded", "tool.done")
    assert [e["turn"] for e in log if e["kind"] in lifecycle] == [1, 1, 1]
    cut, _, _ = _of(log, "turn.closed")
    # Cut while the tool ran: the text before the call is whole, so nothing was cut off.
    assert cut == {"outcome": "cut", "error": None}
    assert [item["source"] for item in _remembered(log)] == [
        "time-line",
        "user",
        "gem",
        "gem",
        "placeholder",
        "user",
        "gem",
        "background-result",
        "background-result",
        "gem",
    ]
    # Each request was sent what was remembered before it, and nothing is written twice:
    # the result's text is in the log once, where the gem remembers it.
    assert [sent["count"] for sent in _of(log, "request.sent")] == [2, 6, 9]
    for number, request in enumerate(setup.llm.requests, start=1):
        assert _sent_before(log, number) == request["input"]
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
        # What the call's requests are made with, before anything the call remembers.
        "settings.changed",
        # The line that opens the call, and the one that gives the time.
        "note.written",
        "note.written",
        # The gem speaks first: a line of ours asks it to.
        "turn.opened",
        "note.written",
        "request.sent",
        "request.done",
        # The greeting is settled when the user next speaks, and their turn opens.
        "gem.said",
        "turn.closed",
        "turn.opened",
        "user.said",
        "request.sent",
        "request.done",
        # The last reply is settled when the call ends.
        "gem.said",
        "turn.closed",
        "call.closed",
    ]
    assert [note["source"] for note in _shown(log, "note.written")] == [
        "call-line",
        "time-line",
        "greeting",
    ]
    # The lines that open the call belong to no turn. The greeting's line does.
    assert [note["turn"] for note in _shown(log, "note.written")] == [None, None, 1]
    assert _shown(log, "note.written")[1]["item"] == TIME_LINE
    opened = _shown(log, "turn.opened")
    assert [(turn["by"], turn["mode"]) for turn in opened] == [("gem", "call"), ("user", "call")]
    assert [said["item"]["content"] for said in _shown(log, "user.said")] == ["what's new?"]
    assert {closed["outcome"] for closed in _shown(log, "turn.closed")} == {"finished"}
    assert [(e["kind"], e["source"], e["mode"]) for e in log if e["kind"] in REMEMBERED] == [
        ("note.written", "call-line", "call"),
        ("note.written", "time-line", "call"),
        ("note.written", "greeting", "call"),
        ("gem.said", "gem", "call"),
        ("user.said", "user", "call"),
        ("gem.said", "gem", "call"),
    ]
    assert [sent["count"] for sent in _shown(log, "request.sent")] == [3, 5]
    await _sent_is_what_the_model_was_given(world.conversations, world.gem.id, world.llm.requests)


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
    assert (cut["turn"], cut["outcome"]) == (1, "cut")
    # What was kept of it is what the gem remembers of that turn, and nowhere else.
    assert [e["item"]["content"] for e in log if e["kind"] == "gem.said" and e["turn"] == 1] == [
        "Once upon a…"
    ]
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
    assert [e["item"] for e in log if e["kind"] in REMEMBERED][:2] == [_CALLED, TIME_LINE]
    assert [e["item"]["content"] for e in log if e["kind"] in REMEMBERED][2:] == [
        "tell me a story",
        "Once upon a…",
        "never mind",
        "Sure.",
    ]
    # The second request was sent the line, the user's words, what was heard of the
    # reply, and what the user said over it.
    assert world.llm.requests[1]["input"] == [
        _CALLED,
        TIME_LINE,
        _user("tell me a story"),
        _gem_said("Once upon a…"),
        _user("never mind"),
    ]
    await _sent_is_what_the_model_was_given(world.conversations, world.gem.id, world.llm.requests)


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
    # The message is written seconds after the call gave the time: it is not given again.
    assert [note["source"] for note in _shown(log, "note.written")] == [
        "call-line",
        "time-line",
        "chat-line",
    ]
    # One change at the first request. The second is made with the same model,
    # instructions and tools, so nothing is said to have changed.
    assert len(_shown(log, "settings.changed")) == 1
    kinds = [entry["kind"] for entry in log]
    assert kinds.index("call.closed") < kinds.index("turn.opened", kinds.index("call.closed"))


async def test_a_call_in_which_nothing_is_said_leaves_its_settings_and_its_opening_line() -> None:
    world = await _world()

    call, _, keeping = await world.call()
    await _hang_up(call, keeping)
    log = await _call_log(world)

    # What its requests would be made with is written, though none was made. It is no
    # part of what the gem remembers, which is the call's opening lines alone: the one
    # that says it was called, and the one that gives the time.
    assert [entry["kind"] for entry in log] == [
        "call.opened",
        "settings.changed",
        "note.written",
        "note.written",
        "call.closed",
    ]
    assert [e["source"] for e in log if e["kind"] in REMEMBERED] == ["call-line", "time-line"]
    assert world.llm.requests == []


async def test_a_change_of_the_way_to_the_provider_in_a_call_is_written_before_its_request() -> (
    None
):
    world = await _world(_spoken("Hi."), _spoken("Sure."))

    call, f, keeping = await world.call()
    await _said_and_heard(f, "hello")
    f.clock.advance(60)
    # The way to the provider changes while the call is open.
    cast("Any", world.llm).via = "proxy"
    await _said_and_heard(f, "and now?")
    await _hang_up(call, keeping)
    log = await _call_log(world)

    first, second = _shown(log, "settings.changed")
    assert first["turn"] is None
    # Only what changed, just before the request it first applies to, in its turn.
    assert {key: value for key, value in second.items() if key not in ("seq", "at")} == {
        "kind": "settings.changed",
        "turn": 2,
        "model": {"name": "6 Luna", "provider": "OpenAI", "via": "proxy"},
    }
    after = log[log.index(second) + 1]
    assert (after["kind"], after["turn"], after["request"]) == ("request.sent", 2, 2)


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
        "settings.changed",
        "note.written",
        "note.written",
        "turn.opened",
        "note.written",
        "turn.closed",
        "call.closed",
    ]
    (closed,) = _shown(log, "turn.closed")
    assert (closed["turn"], closed["outcome"]) == (1, "cut")
    # Nothing was said. What is remembered is our three lines: the call, the time, and
    # the greeting that was asked for.
    assert [e["source"] for e in log if e["kind"] in REMEMBERED] == [
        "call-line",
        "time-line",
        "greeting",
    ]


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
    # Cut, with nothing delivered: the model produced it, and the gem remembers none of it.
    assert greeting["outcome"] == "cut"
    assert [e for e in log if e["kind"] == "gem.said" and e["turn"] == greeting["turn"]] == []
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


def _searching() -> tuple[asyncio.Event, list[Any]]:
    """A round that says a word and searches, and the gate its search waits for."""
    search_call = _search_call("weather in Pune")
    return asyncio.Event(), [_delta("Let me look."), _starts(search_call), _completed(search_call)]


async def _result_waits(world: _World) -> None:
    """Wait until the gem's search has ended in the background and its result waits."""
    late = world.conversations._kept[str(world.gem.id)].late
    async with asyncio.timeout(2):
        while True:
            if late.ready:
                return
            await asyncio.sleep(0)


_LIFE = ("tool.started", "tool.backgrounded", "tool.done")


async def test_a_search_that_goes_to_the_background_in_a_call_is_told_with_the_next_turn() -> None:
    finish, searching = _searching()
    world = await _world(searching, _spoken("It is sunny in Pune."), search=_Search(gate=finish))

    call, f, keeping = await world.call()
    f.stt.say("weather in Pune?")
    await _read_until(f.conversation, "tool.call")
    # The user speaks while the search runs. The reply is settled, and the search
    # carries on in the background, where it ends before they have finished speaking.
    f.stt.turn_start()
    await _read_until(f.conversation, "interrupt")
    finish.set()
    await _result_waits(world)
    f.stt.turn_end("are you there?")
    await _read_until(f.conversation, "assistant.done")
    f.clock.advance(60)
    await _hang_up(call, keeping)
    log = await _call_log(world)

    # The whole of the search, in the order of time, each entry naming the turn that
    # made the call.
    assert [(e["kind"], e["turn"]) for e in log if e["kind"] in _LIFE] == [
        ("tool.started", 1),
        ("tool.backgrounded", 1),
        ("tool.done", 1),
    ]
    (ended,) = _shown(log, "tool.done")
    assert (ended["outcome"], ended["background"]) == ("done", True)
    # It is delivered in the turn that tells it: a stub call and its result, ahead of
    # what the user said in that turn, each naming the call whose result they carry.
    delivered = [e for e in log if e.get("delivers")]
    assert [(e["kind"], e["turn"], e["source"], e["delivers"]) for e in delivered] == [
        ("tool.called", 2, "background-result", "call_1"),
        ("tool.result", 2, "background-result", "call_1"),
    ]
    assert delivered[1]["item"]["output"] == ended["result"]
    second = [e["kind"] for e in log if e["turn"] == 2]
    assert second[:4] == ["turn.opened", "tool.called", "tool.result", "user.said"]
    await _sent_is_what_the_model_was_given(world.conversations, world.gem.id, world.llm.requests)


async def test_a_result_that_comes_when_nothing_is_said_is_told_in_a_turn_of_its_own() -> None:
    finish, searching = _searching()
    world = await _world(
        searching,
        _spoken("Hello."),
        _spoken("It is sunny in Pune."),
        search=_Search(gate=finish),
    )

    call, f, keeping = await world.call()
    f.stt.say("weather in Pune?")
    await _read_until(f.conversation, "tool.call")
    f.stt.turn_start()
    await _read_until(f.conversation, "interrupt")
    f.stt.turn_end("hello?")
    await _read_until(f.conversation, "assistant.done")
    # That reply has been heard to its end when the search comes back.
    f.clock.advance(60)
    finish.set()
    await _read_until(f.conversation, "assistant.done")
    f.clock.advance(60)
    await _hang_up(call, keeping)
    log = await _call_log(world)

    opened = _shown(log, "turn.opened")
    assert [(turn["turn"], turn["by"]) for turn in opened] == [
        (1, "user"),
        (2, "user"),
        (3, "background_result"),
    ]
    assert [(e["kind"], e["turn"]) for e in log if e["kind"] in _LIFE] == [
        ("tool.started", 1),
        ("tool.backgrounded", 1),
        ("tool.done", 1),
    ]
    # The turn that tells it opens, then the stub and its result, then the request.
    third = [e["kind"] for e in log if e["turn"] == 3 and e["kind"] not in _LIFE]
    assert third == [
        "turn.opened",
        "tool.called",
        "tool.result",
        "request.sent",
        "request.done",
        "gem.said",
        "turn.closed",
    ]
    assert {e["delivers"] for e in log if e.get("delivers")} == {"call_1"}
    await _sent_is_what_the_model_was_given(world.conversations, world.gem.id, world.llm.requests)


async def test_a_sound_with_no_words_is_remembered_as_the_line_that_says_so() -> None:
    world = await _world(
        _spoken("Once upon a time there was a dragon."), _spoken("As I was saying.")
    )

    call, f, keeping = await world.call()
    f.stt.say("tell me a story")
    await _read_until(f.conversation, "assistant.word")
    await _let_audio_out()
    f.clock.advance(_DELAY + 2.5)
    # The recognizer hears something while the reply plays, and makes no words of it.
    f.stt.turn_start()
    await _read_until(f.conversation, "interrupt")
    f.stt.turn_end("")
    await _read_until(f.conversation, "assistant.done")
    f.clock.advance(60)
    await _hang_up(call, keeping)
    log = await _call_log(world)

    notes = _shown(log, "note.written")
    assert [(note["source"], note["turn"]) for note in notes] == [
        ("call-line", None),
        ("time-line", None),
        ("no-words", 2),
    ]
    # The line is remembered before the request it is the last thing of.
    kinds = [e["kind"] for e in log if e["turn"] == 2]
    assert kinds[:3] == ["turn.opened", "note.written", "request.sent"]
    await _sent_is_what_the_model_was_given(world.conversations, world.gem.id, world.llm.requests)


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
    # The second message is remembered, in a turn that asked the model nothing.
    assert turns[2] == ["turn.opened", "user.said", "turn.closed"]
    assert [closed["outcome"] for closed in _of(log, "turn.closed")] == ["cut", "cut", "finished"]
    assert [said["item"]["content"] for said in _of(log, "user.said")] == ["one", "two", "three"]
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
    assert [entry["seq"] for entry in whole] == list(range(14))
    assert len(latest) == 5
    assert [entry["kind"] for entry in latest] == [
        "user.said",
        "request.sent",
        "request.done",
        "gem.said",
        "turn.closed",
    ]
    # An entry is its own fields beside the four every entry has.
    assert whole[1]["kind"] == "note.written"
    opened = whole[2]
    assert opened == {
        "seq": 2,
        "at": opened["at"],
        "kind": "turn.opened",
        "turn": 1,
        "by": "user",
        "mode": "chat",
    }
    # One that is remembered has the same shape: its item is among its own fields.
    said = latest[-2]
    assert said == {
        "seq": 12,
        "at": said["at"],
        "kind": "gem.said",
        "turn": 2,
        "item": {"role": "assistant", "content": "Hi again."},
        "mode": "chat",
        "source": "gem",
    }
    assert await gems.get_log_entry(gem_id, 2) == opened
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
    tools = [{"name": f"tool_{n}", "description": "A tool."} for n in range(80)]

    trimmed = _trimmed({"seq": 3, "kind": "settings.changed", "tools": tools})
    whole = _trimmed({"seq": 4, "kind": "settings.changed", "tools": tools[:2]})

    assert (len(trimmed["tools"]), trimmed["partial"]) == (50, True)
    assert trimmed["tools"] == tools[:50]
    assert "partial" not in whole


async def test_a_socket_that_names_no_gem_watches_every_gem() -> None:
    setup = await _chat(_said("Hi."))
    socket, linked = await _open(_watching(setup))

    socket.raw(json.dumps({"type": "log.watch"}))
    socket.send(setup.gem.id, "hello")
    await socket.until("log.entry", 8)

    assert [entry["kind"] for entry in _entries(socket)][:4] == [
        "settings.changed",
        "note.written",
        "turn.opened",
        "user.said",
    ]
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
    assert (len(said["item"]["content"]), said["partial"]) == (4000, True)
    # An entry that was not cut says nothing of it.
    assert "partial" not in _entries(socket)[0]
    gems = GemsServiceV1Class(setup.gems, setup.conversations)
    whole = await gems.get_log_entry(gem_id, said["seq"])
    assert whole["item"]["content"] == long and "partial" not in whole
    await _close(socket, linked)


async def test_a_page_comes_with_the_settings_in_force_before_it() -> None:
    setup = await _chat(_said("Hi."), _said("Hi again."))
    socket, linked = await _open(setup.service())
    for count, text in enumerate(("hello", "hello again"), start=1):
        socket.send(setup.gem.id, text)
        await socket.until("chat.done", count)
    gems = GemsServiceV1Class(setup.gems, setup.conversations)
    gem_id = str(setup.gem.id)

    whole = await gems.get_log(gem_id, before=None, limit=500)
    last = await gems.get_log(gem_id, before=None, limit=3)

    # Nothing was written down before the first page: every part is unknown.
    assert whole.settings == {"model": None, "instructions": None, "tools": None}
    # A page that starts mid-log is told what its requests were made with.
    (changed,) = (e for e in whole.entries if e["kind"] == "settings.changed")
    assert last.settings == {
        "model": changed["model"],
        "instructions": changed["instructions"],
        "tools": changed["tools"],
    }
    await _close(socket, linked)


async def test_what_a_request_was_sent_is_what_the_gem_remembered_before_it() -> None:
    searches = _Searches(_WEATHER)
    setup = await _chat(_looks(), _said("Noon."), _said("It is sunny in Pune."), search=searches)
    socket, linked = await _open(setup.service())
    await _cut_while_searching(setup, socket)
    searches.gates[_WEATHER].set()
    await socket.until("chat.done", 2)
    gems = GemsServiceV1Class(setup.gems, setup.conversations)
    gem_id = str(setup.gem.id)
    log = (await gems.get_log(gem_id, before=None, limit=500)).entries
    requests = [entry for entry in log if entry["kind"] == "request.sent"]

    sent = [await gems.get_log_sent(gem_id, request["seq"]) for request in requests]

    # Each is exactly what the model was sent, whatever came and went in between.
    assert [one["items"] for one in sent] == [r["input"] for r in setup.llm.requests]
    assert [one["exact"] for one in sent] == [True, True, True]
    assert [one["request"] for one in sent] == [1, 2, 3]
    assert (
        "".join(layer["text"] for layer in sent[2]["instructions"])
        == (setup.llm.requests[2]["instructions"])
    )
    assert sent[2]["model"] == {"name": "6 Luna", "provider": "OpenAI", "via": "api"}
    assert [tool["name"] for tool in sent[2]["tools"]] == ["web_search", "cancel_tool_call"]
    # An entry that is not a request has nothing it was sent.
    with pytest.raises(NotFoundError):
        await gems.get_log_sent(gem_id, requests[0]["seq"] + 1)
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
