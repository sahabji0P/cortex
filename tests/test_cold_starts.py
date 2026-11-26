"""A gem's conversation starting cold, in writing and in a call, and what its log shows.

The clock is set (`set_clock`), so the tests say how long the gem was quiet. The gems
use an OpenAI model, whose cache is held to last 30 minutes, unless a test says
otherwise. The searches return a long page, so their results can be expired.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.entities.call_ent import CallFault
from app.entities.cold_start import EXPIRED, KEEP_UNDER
from app.entities.conversation_store import LogEntry
from app.routers.gems.service import GemsServiceV1Class
from tests.test_chat import _chat, _close, _kept, _open, _said
from tests.test_one_conversation import _hang_up, _said_and_heard, _spoken, _world
from tests.test_voice_conversation import _completed, _Search, _search_call, _starts
from tests.time_lines import set_clock

_START = datetime(2026, 11, 26, 9, 0, tzinfo=UTC)
_PAGE = "The race went on for a long time. " * (KEEP_UNDER // 30)


class _LongSearch(_Search):
    """Web search that finds one long page."""

    async def search(self, *, objective: str, queries: list[str], mode: str) -> Any:
        found = await super().search(objective=objective, queries=queries, mode=mode)
        found.results[0].excerpts = [_PAGE]
        return found


def _looks(query: str, call_id: str) -> list[Any]:
    """A round that searches for `query`."""
    call = _search_call(query, call_id)
    return [_starts(call), _completed(call)]


def _output(call_id: str, output: str) -> dict[str, str]:
    return {"type": "function_call_output", "call_id": call_id, "output": output}


def _results(request: dict[str, Any]) -> list[str]:
    """The outputs of the results a request was sent, in order."""
    return [
        item["output"] for item in request["input"] if item.get("type") == "function_call_output"
    ]


async def _log(setup: Any) -> list[dict[str, Any]]:
    service = GemsServiceV1Class(setup.gems, setup.conversations)
    return (await service.get_log(str(setup.gem.id), before=None, limit=500)).entries


def _of(log: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [entry for entry in log if entry["kind"] == kind]


async def _searched_twice(monkeypatch: pytest.MonkeyPatch, **gem: Any) -> Any:
    """A gem that has searched twice in its first reply, then answered."""
    move = set_clock(monkeypatch, _START)
    setup = await _chat(
        _looks("first", "call_a"),
        _looks("second", "call_b"),
        _said("Found it."),
        search=_LongSearch(),
        **gem,
    )
    setup.move = move
    socket, linked = await _open(setup.service())
    socket.send(setup.gem.id, "look it up")
    await socket.until("chat.done")
    setup.socket, setup.linked = socket, linked
    return setup


async def test_a_new_gem_is_not_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    setup = await _searched_twice(monkeypatch)

    assert _of(await _log(setup), "cache.cold") == []
    # Each request started with the whole of the one before it.
    assert [len(_results(request)) for request in setup.llm.requests] == [0, 1, 2]
    await _close(setup.socket, setup.linked)


async def test_two_replies_within_the_lifetime_keep_every_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _searched_twice(monkeypatch)
    setup.llm.then(_said("Yes."))
    setup.move(_START + timedelta(minutes=29))

    setup.socket.send(setup.gem.id, "thanks")
    await setup.socket.until("chat.done", 2)

    last, warm = setup.llm.requests[-2:]
    sent_before = json.dumps(last["input"])[:-1]
    assert json.dumps(warm["input"]).startswith(sent_before)
    assert _of(await _log(setup), "cache.cold") == []
    await _close(setup.socket, setup.linked)


async def test_a_reply_after_the_lifetime_starts_cold_and_expires_earlier_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _searched_twice(monkeypatch)
    stored_before = await setup.stored()
    setup.llm.then(_looks("third", "call_c"), _said("Yes."))
    setup.move(_START + timedelta(minutes=31))

    setup.llm.starts_cold()
    setup.socket.send(setup.gem.id, "and now?")
    await setup.socket.until("chat.done", 2)

    log = await _log(setup)
    [cold] = _of(log, "cache.cold")
    last_sent = _of([entry for entry in log if entry["seq"] < cold["seq"]], "request.sent")[-1]
    assert (cold["reason"], cold["before"], cold["turn"]) == ("quiet", last_sent["seq"], None)
    # It is said before the reply's turn opens.
    assert cold["seq"] < _of(log, "turn.opened")[-1]["seq"]
    cold_request, warm = setup.llm.requests[-2:]
    assert _results(cold_request) == [EXPIRED, EXPIRED]
    # The search made after the cold start is sent whole, and the requests after it in
    # the reply start with the whole of the one before.
    assert _results(warm)[:2] == [EXPIRED, EXPIRED]
    assert _PAGE in _results(warm)[2]
    assert json.dumps(warm["input"]).startswith(json.dumps(cold_request["input"])[:-1])
    # What is stored is not changed.
    assert (await setup.stored())[: len(stored_before)] == stored_before
    await _close(setup.socket, setup.linked)


async def test_after_a_cold_start_the_next_reply_is_sent_the_same_expired_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _searched_twice(monkeypatch)
    setup.llm.then(_said("Yes."), _said("Sure."))
    setup.move(_START + timedelta(minutes=31))
    setup.llm.starts_cold()
    setup.socket.send(setup.gem.id, "and now?")
    await setup.socket.until("chat.done", 2)

    # A restart: what is sent is read from what is stored.
    again = setup.again()
    socket, linked = await _open(again.service())
    socket.send(setup.gem.id, "one more")
    await socket.until("chat.done")

    cold_request, warm = setup.llm.requests[-2:]
    assert json.dumps(warm["input"]).startswith(json.dumps(cold_request["input"])[:-1])
    assert _results(warm) == [EXPIRED, EXPIRED]
    assert len(_of(await _log(setup), "cache.cold")) == 1
    await _close(socket, linked)
    await _close(setup.socket, setup.linked)


async def test_a_change_of_settings_starts_cold_however_soon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _searched_twice(monkeypatch)
    setup.llm.then(_said("Yes."))
    await setup.gems.update(str(setup.gem.id), {"instructions": "You are terse."})

    setup.llm.starts_cold()
    setup.socket.send(setup.gem.id, "and now?")
    await setup.socket.until("chat.done", 2)

    [cold] = _of(await _log(setup), "cache.cold")
    assert cold["reason"] == "settings"
    assert _results(setup.llm.requests[-1]) == [EXPIRED, EXPIRED]
    await _close(setup.socket, setup.linked)


async def test_a_reset_gem_is_not_cold_until_it_has_had_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _searched_twice(monkeypatch)
    setup.llm.then(_said("Yes."), _looks("again", "call_d"), _said("Here."), _said("Ok."))
    setup.move(_START + timedelta(minutes=31))
    setup.llm.starts_cold()
    setup.socket.send(setup.gem.id, "and now?")
    await setup.socket.until("chat.done", 2)

    await setup.conversations.reset(str(setup.gem.id))
    setup.move(_START + timedelta(hours=2))
    setup.socket.send(setup.gem.id, "hello")
    await setup.socket.until("chat.done", 3)
    # A long time later still: the reset gem has had requests, so it starts cold.
    setup.move(_START + timedelta(hours=3))
    setup.llm.starts_cold()
    setup.socket.send(setup.gem.id, "hello again")
    await setup.socket.until("chat.done", 4)

    log = await _log(setup)
    reset = _of(log, "gem.reset")[0]["seq"]
    colds = _of(log, "cache.cold")
    assert len(colds) == 2
    # Its line is a request since the reset: the line before it is forgotten.
    assert colds[1]["before"] > reset
    # The reply right after the reset was sent its search whole.
    assert _PAGE in _results(setup.llm.requests[-2])[0]
    assert _results(setup.llm.requests[-1]) == [EXPIRED]
    await _close(setup.socket, setup.linked)


async def test_what_a_request_was_sent_is_read_back_expired_and_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _searched_twice(monkeypatch)
    setup.llm.then(_said("Yes."))
    setup.move(_START + timedelta(minutes=31))
    setup.llm.starts_cold()
    setup.socket.send(setup.gem.id, "and now?")
    await setup.socket.until("chat.done", 2)

    log = await _log(setup)
    first, *_, last = _of(log, "request.sent")
    service = GemsServiceV1Class(setup.gems, setup.conversations)
    sent = await service.get_log_sent(str(setup.gem.id), last["seq"])
    before = await service.get_log_sent(str(setup.gem.id), _of(log, "request.sent")[2]["seq"])

    assert sent["exact"] and before["exact"]
    # As the model was sent it, item for item.
    assert sent["items"] == setup.llm.requests[-1]["input"]
    assert _results(setup.llm.requests[-1]) == [EXPIRED, EXPIRED]
    # A request before the cold start was sent them whole.
    assert before["items"] == setup.llm.requests[2]["input"]
    assert all(_PAGE in output for output in _results({"input": before["items"]}))
    assert first["seq"] < last["seq"]
    await _close(setup.socket, setup.linked)


async def test_what_was_stored_after_the_last_request_is_sent_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A log as a reply cut right after its search finished leaves it, and a search
    # left running that a restart lost: their results were stored after the last
    # request, so no request the cache could hold has them.
    move = set_clock(monkeypatch, _START)
    setup = await _chat(_said("Here."), search=_LongSearch())
    gem_id = setup.gem.id
    call_a, call_b, call_c = (_search_call("q", name) for name in ("call_a", "call_b", "call_c"))
    await setup.store.write(
        [
            _kept(gem_id, 0, {"role": "user", "content": "look it up"}),
            LogEntry(gem_id=gem_id, kind="request.sent", seq=1, at=_START, data={"count": 1}),
            _kept(gem_id, 2, dict(vars(call_a))),
            _kept(gem_id, 3, _output("call_a", _PAGE)),
            LogEntry(gem_id=gem_id, kind="request.sent", seq=4, at=_START, data={"count": 3}),
            _kept(gem_id, 5, {"role": "assistant", "content": "Found."}),
            _kept(gem_id, 6, dict(vars(call_b))),
            _kept(gem_id, 7, _output("call_b", _PAGE)),
            _kept(gem_id, 8, dict(vars(call_c))),
            _kept(gem_id, 9, _output("call_c", "Still looking."), mark="placeholder"),
        ]
    )
    move(_START + timedelta(minutes=31))
    socket, linked = await _open(setup.service())

    socket.send(gem_id, "anything?")
    await socket.until("chat.done")

    [cold] = _of(await _log(setup), "cache.cold")
    assert cold["before"] == 4
    [request] = setup.llm.requests
    by_call = {
        item["call_id"]: item["output"]
        for item in request["input"]
        if item.get("type") == "function_call_output"
    }
    assert by_call["call_a"] == EXPIRED
    assert by_call["call_b"] == _PAGE
    assert by_call["call_c"] == "Still looking."
    # The lost call is closed at load, after the last request, and that goes whole too.
    [stub] = [output for call_id, output in by_call.items() if call_id.startswith("late_")]
    assert stub.startswith("This call was lost")
    await _close(socket, linked)


async def test_a_cold_start_that_cannot_be_stored_fails_the_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _searched_twice(monkeypatch)
    setup.llm.then(_said("Yes."))
    setup.move(_START + timedelta(minutes=31))
    stored_before = await setup.stored()
    setup.docdb.breaks_after = 0

    setup.socket.send(setup.gem.id, "and now?")
    await setup.socket.until("chat.error")
    setup.docdb.breaks_after = None

    # No request was sent by it, and nothing after it was stored.
    assert len(setup.llm.requests) == 3
    assert await setup.stored() == stored_before
    assert _of(await _log(setup), "cache.cold") == []
    await _close(setup.socket, setup.linked)


# ---- in a call


async def _called_after_searching(monkeypatch: pytest.MonkeyPatch, quiet: timedelta) -> Any:
    """A gem that searched in writing, then is called after being quiet for `quiet`."""
    move = set_clock(monkeypatch, _START)
    world = await _world(
        _looks("first", "call_a"), _said("Found it."), _spoken("Hello."), search=_LongSearch()
    )
    await world.write("look it up")
    move(_START + quiet)
    return world


async def test_a_call_after_the_lifetime_starts_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    world = await _called_after_searching(monkeypatch, timedelta(minutes=31))

    world.llm.starts_cold()
    call, f, keeping = await world.call()
    await _said_and_heard(f, "hello")
    await _hang_up(call, keeping)

    colds = await world.store.of_kind(world.gem.id, "cache.cold")
    assert [cold.data["reason"] for cold in colds] == ["quiet"]
    assert _results(world.llm.requests[-1]) == [EXPIRED]


async def test_a_call_within_the_lifetime_keeps_every_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = await _called_after_searching(monkeypatch, timedelta(minutes=10))

    call, f, keeping = await world.call()
    await _said_and_heard(f, "hello")
    await _hang_up(call, keeping)

    assert await world.store.of_kind(world.gem.id, "cache.cold") == []
    assert _PAGE in _results(world.llm.requests[-1])[0]


async def test_a_call_whose_cold_start_cannot_be_stored_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = await _called_after_searching(monkeypatch, timedelta(minutes=31))
    docdb = world.store._docdb

    stored_before = await world.stored()

    call, _, keeping = await world.call()
    docdb.breaks_after = 0
    with pytest.raises(CallFault):
        async with asyncio.timeout(2):
            await keeping
    with pytest.raises(CallFault):
        await call.close()
    docdb.breaks_after = None

    # The call failed, and nothing it added is stored: not its cold start, not its line.
    assert await world.store.of_kind(world.gem.id, "cache.cold") == []
    assert await world.stored() == stored_before
