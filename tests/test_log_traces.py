"""A gem's log, as whole conversations leave it: written here, and spoken further down."""

import asyncio
from types import SimpleNamespace
from typing import Any

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
