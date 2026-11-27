"""Compacting a gem's conversation, when the user asks.

The clock is set (`set_clock`), and the gems use an OpenAI model, whose cache is held to
last 30 minutes. A compaction sums up everything the gem remembers before its own
request.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from bson import ObjectId

from app.core.errors import ConflictError, NotFoundError, UpstreamError
from app.entities import compaction_ent, prices
from app.entities.compaction import (
    INSTRUCTIONS,
    SCHEMA,
    SUMMARY_TOKENS,
    Compacted,
    asked,
    costs,
    summed_up,
    written,
)
from app.entities.compaction_ent import CompactionEntityClass
from app.entities.darbar_ent import DarbarEntityClass
from app.entities.model import Structured
from app.main import app
from app.routers.gems.service import GemsServiceV1Class, get_gems_service_v1
from app.routers.link.service import LinkServiceV1Class
from tests.test_chat import (
    _chat,
    _close,
    _eventually_asked,
    _open,
    _said,
)
from tests.test_gem_events import _came_in
from tests.test_link_call import _close as _close_link
from tests.test_link_call import _open as _open_link
from tests.test_link_call import _Setup as _CallSetup
from tests.test_link_call import _Voice
from tests.test_voice_conversation import _completed, _delta
from tests.time_lines import TIME_LINE, set_clock

_START = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)
# What the model is made to write: one point under the first heading.
_POINT = "To save."
# What the user said in `_a_new_sitting`.
_SAID = ["message 1", "message 2", "message 3"]


def _fields(point: str) -> dict[str, list[str]]:
    return {
        "user_wants": [point],
        "preferences": [],
        "decisions": [],
        "done": [],
        "open": [],
        "exact_details": [],
        "unverified": [],
    }


def _summed(point: str = _POINT, said: list[str] = _SAID) -> str:
    """The summary stored from `point`, of a conversation with no identifiers in it."""
    return written(_fields(point), [], said)


_SUMMARY = _summed()


def _summary(point: str = _POINT, *, used: bool = False, text: str | None = None) -> list[Any]:
    """The summary request's stream: the JSON of `point`, or `text` as it is."""
    done = _completed()
    if used:
        done.response.usage = SimpleNamespace(
            input_tokens=3000,
            input_tokens_details=SimpleNamespace(cached_tokens=0, cache_write_tokens=0),
            output_tokens=120,
            output_tokens_details=SimpleNamespace(reasoning_tokens=0),
        )
    return [_delta(json.dumps(_fields(point)) if text is None else text), done]


async def _compactions(setup: Any) -> CompactionEntityClass:
    adapter = cast("Any", setup.store)._docdb
    return CompactionEntityClass(
        gems=setup.gems,
        darbar=DarbarEntityClass(adapter),
        store=setup.store,
        conversations=setup.conversations,
        chat=setup.chat,
    )


async def _quiet(entity: CompactionEntityClass) -> None:
    """Wait until what the entity runs on its own after a reply is done."""
    for _ in range(10):
        await asyncio.sleep(0)
    while entity._running:
        await asyncio.gather(*list(entity._running), return_exceptions=True)
        for _ in range(10):
            await asyncio.sleep(0)


async def _a_new_sitting(monkeypatch: pytest.MonkeyPatch, **gem: Any) -> Any:
    """A gem that answered two messages, then, after its cache expired, a third."""
    move = set_clock(monkeypatch, _START)
    setup = await _chat(_said("One."), _said("Two."), _said("Three."), **gem)
    setup.compactions = await _compactions(setup)
    setup.socket, setup.linked = await _open(setup.service())
    setup.move = move
    for count in (1, 2):
        setup.socket.send(setup.gem.id, f"message {count}")
        await setup.socket.until("chat.done", count)
    move(_START + timedelta(minutes=31))
    setup.llm.starts_cold()
    setup.socket.send(setup.gem.id, "message 3")
    await setup.socket.until("chat.done", 3)
    await _quiet(setup.compactions)
    return setup


async def _log(setup: Any) -> list[dict[str, Any]]:
    service = GemsServiceV1Class(setup.gems, setup.conversations)
    return (await service.get_log(str(setup.gem.id), before=None, limit=500)).entries


def _of(log: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [entry for entry in log if entry["kind"] == kind]


async def _asked_at(setup: Any) -> int:
    """The number of the latest compaction's own `request.sent`: its line."""
    log = await _log(setup)
    turn = [e for e in _of(log, "turn.opened") if e["by"] == "compaction"][-1]["turn"]
    return next(e["seq"] for e in _of(log, "request.sent") if e["turn"] == turn)


# ---- the user's compaction


async def test_a_compaction_sums_up_everything_before_its_own_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _a_new_sitting(monkeypatch)
    setup.llm.then(_summary(used=True), _said("Four."))

    compacted = await setup.compactions.compact(str(setup.gem.id))

    assert compacted == Compacted(_SUMMARY, await _asked_at(setup))
    # The summary request was sent what the next reply would be, then a plain message
    # that asks for it, as one call: our instructions, tool use off, JSON to a schema.
    summary_request = setup.llm.requests[-1]
    assert summary_request["input"][:-1] == setup.llm.requests[-2]["input"] + [
        {"role": "assistant", "content": "Three."}
    ]
    assert summary_request["input"][-1] == {
        "role": "user",
        "content": "Summarise the conversation above.",
    }
    assert summary_request["instructions"] == INSTRUCTIONS
    assert summary_request["instructions"] != setup.llm.requests[-2]["instructions"]
    assert summary_request["tool_choice"] == "none"
    assert summary_request["tools"] == setup.llm.requests[-2]["tools"]
    assert summary_request["text"]["format"]["schema"] == SCHEMA
    # The next reply is sent the summary, then only what came after it: the line that
    # gives the time (the one before is summed up), and the message.
    setup.move(_START + timedelta(minutes=33))
    setup.socket.send(setup.gem.id, "message 4")
    await setup.socket.until("chat.done", 4)
    assert setup.llm.requests[-1]["input"] == [
        summed_up(_SUMMARY),
        TIME_LINE,
        {"role": "user", "content": "message 4"},
    ]
    await _close(setup.socket, setup.linked)


async def test_the_first_request_after_a_compaction_starts_cold_for_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _a_new_sitting(monkeypatch)
    setup.llm.then(_summary(), _said("Four."), _said("Five."))
    compacted = await setup.compactions.compact(str(setup.gem.id))
    colds = len(_of(await _log(setup), "cache.cold"))

    # Straight after it, with the cache warm.
    setup.socket.send(setup.gem.id, "message 4")
    await setup.socket.until("chat.done", 4)
    setup.socket.send(setup.gem.id, "message 5")
    await setup.socket.until("chat.done", 5)

    log = await _log(setup)
    [cold] = _of(log, "cache.cold")[colds:]
    assert (cold["reason"], cold["before"]) == ("compaction", compacted.through)
    [stored] = _of(log, "context.compacted")
    assert stored["seq"] < cold["seq"] < _of(log, "request.sent")[-2]["seq"]
    await _close(setup.socket, setup.linked)


async def test_a_change_of_settings_after_a_compaction_is_the_reason_it_starts_cold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _a_new_sitting(monkeypatch)
    setup.llm.then(_summary(), _said("Four."))
    await setup.compactions.compact(str(setup.gem.id))
    await setup.gems.update(str(setup.gem.id), {"instructions": "You are terse."})

    setup.socket.send(setup.gem.id, "message 4")
    await setup.socket.until("chat.done", 4)

    assert _of(await _log(setup), "cache.cold")[-1]["reason"] == "settings"
    await _close(setup.socket, setup.linked)


async def test_a_compaction_is_written_down_in_a_turn_of_its_own_with_its_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _a_new_sitting(monkeypatch)
    setup.llm.then(_summary(used=True))

    await setup.compactions.compact(str(setup.gem.id))

    log = await _log(setup)
    opened = _of(log, "turn.opened")[-1]
    [done] = [e for e in _of(log, "request.done") if e["turn"] == opened["turn"]]
    [compacted] = _of(log, "context.compacted")
    closed = _of(log, "turn.closed")[-1]
    assert (opened["by"], opened["mode"]) == ("compaction", "chat")
    assert done["usage"] == {
        "input": 3000,
        "cache_read": 0,
        "cache_write": 0,
        "output": 120,
        "thinking": 0,
    }
    assert done["cost_usd"] is not None and done["cost_usd"] > 0
    assert done["text"] == json.dumps(_fields(_POINT))
    assert (compacted["turn"], compacted["by"]) == (opened["turn"], "user")
    assert compacted["summary"] == _SUMMARY
    assert (closed["turn"], closed["outcome"]) == (opened["turn"], "finished")
    await _close(setup.socket, setup.linked)


async def test_the_summary_request_has_room_to_write_more_than_a_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _a_new_sitting(monkeypatch)
    setup.llm.then(_summary())
    asked: list[dict[str, Any]] = []
    made = setup.chat.next_request

    async def next_request(*args: Any, **kwargs: Any) -> Any:
        found = await made(*args, **kwargs)
        sent = found.model.round

        def round_(items: Any, **options: Any) -> Any:
            asked.append(options)
            return sent(items, **options)

        found.model.round = round_
        return found

    monkeypatch.setattr(setup.chat, "next_request", next_request)
    await setup.compactions.compact(str(setup.gem.id))

    [options] = asked
    assert options["structured"] == Structured("summary", SCHEMA, SUMMARY_TOKENS)
    assert (options["instructions"], options["think"]) == (INSTRUCTIONS, False)
    await _close(setup.socket, setup.linked)


async def test_what_the_summary_request_was_sent_is_read_back_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _a_new_sitting(monkeypatch)
    setup.llm.then(_summary())
    await setup.compactions.compact(str(setup.gem.id))

    [sent] = [
        e
        for e in _of(await _log(setup), "request.sent")
        if e["turn"] == _of(await _log(setup), "turn.opened")[-1]["turn"]
    ]
    service = GemsServiceV1Class(setup.gems, setup.conversations)
    read = await service.get_log_sent(str(setup.gem.id), sent["seq"])

    assert read["exact"]
    assert read["items"] == setup.llm.requests[-1]["input"]
    assert read["items"][-1] == asked()
    # Our instructions, not the gem's. On OpenAI it is told of the tools in force.
    assert read["instructions"] == [{"name": "compaction", "text": INSTRUCTIONS}]
    reply = [e for e in _of(await _log(setup), "request.sent") if e["seq"] < sent["seq"]][-1]
    assert read["tools"] == (await service.get_log_sent(str(setup.gem.id), reply["seq"]))["tools"]
    await _close(setup.socket, setup.linked)


async def test_identifiers_of_what_is_summed_up_are_copied_after_the_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    move = set_clock(monkeypatch, _START)
    setup = await _chat(_said("Sent to https://pay.example.com/r/AB12CD."), _said("Yes."))
    setup.compactions = await _compactions(setup)
    socket, linked = await _open(setup.service())
    socket.send(setup.gem.id, "pay INV-77812")
    await socket.until("chat.done")
    move(_START + timedelta(minutes=31))
    setup.llm.starts_cold()
    socket.send(setup.gem.id, "thanks")
    await socket.until("chat.done", 2)
    setup.llm.then(_summary())

    compacted = await setup.compactions.compact(str(setup.gem.id))

    copied = compacted.summary.split("\n\n")[-2]
    assert copied.splitlines()[-2:] == [
        "- INV-77812",
        "- https://pay.example.com/r/AB12CD",
    ]
    await _close(socket, linked)


async def test_a_cold_gem_is_compacted_whole_and_starts_cold_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _a_new_sitting(monkeypatch)
    setup.move(_START + timedelta(minutes=31 + 31))
    setup.llm.then(_summary())
    setup.llm.starts_cold()

    compacted = await setup.compactions.compact(str(setup.gem.id))

    log = await _log(setup)
    opened = _of(log, "turn.opened")[-1]
    colds = _of(log, "cache.cold")
    assert compacted.through == await _asked_at(setup)
    # The compaction's request is the gem's next: it started cold, first, for the
    # reason it did.
    assert len(colds) == 2
    assert colds[-1]["seq"] < opened["seq"]
    assert colds[-1]["reason"] == "quiet"
    [sent] = [e for e in _of(log, "request.sent") if e["turn"] == opened["turn"]]
    service = GemsServiceV1Class(setup.gems, setup.conversations)
    assert (await service.get_log_sent(str(setup.gem.id), sent["seq"]))["exact"]
    await _close(setup.socket, setup.linked)


async def test_a_cold_gem_whose_summary_failed_is_compacted_the_same_when_asked_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _a_new_sitting(monkeypatch)
    setup.move(_START + timedelta(minutes=31 + 31))
    failed = SimpleNamespace(
        type="response.failed",
        response=SimpleNamespace(error=SimpleNamespace(message="overloaded")),
    )
    setup.llm.then([failed], _summary())
    setup.llm.starts_cold()

    with pytest.raises(UpstreamError):
        await setup.compactions.compact(str(setup.gem.id))
    # The cold start it wrote stays, and the second is sent the same as the first.
    compacted = await setup.compactions.compact(str(setup.gem.id))

    assert compacted.through == await _asked_at(setup)
    assert setup.llm.requests[-1]["input"] == setup.llm.requests[-2]["input"]
    await _close(setup.socket, setup.linked)


async def test_a_second_compaction_sums_up_the_first_and_moves_the_line_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _a_new_sitting(monkeypatch)
    setup.llm.then(_summary("First."), _said("Four."), _summary("Second."), _said("Five."))
    first = await setup.compactions.compact(str(setup.gem.id))
    setup.move(_START + timedelta(minutes=31 + 31))
    setup.llm.starts_cold()
    setup.socket.send(setup.gem.id, "message 4")
    await setup.socket.until("chat.done", 4)

    second = await setup.compactions.compact(str(setup.gem.id))
    setup.socket.send(setup.gem.id, "message 5")
    await setup.socket.until("chat.done", 5)

    assert second.through > first.through
    # The second summary request was sent the first summary, to sum up with the rest.
    summary_request = setup.llm.requests[-2]
    assert summary_request["input"][0] == summed_up(first.summary)
    assert setup.llm.requests[-1]["input"][0] == summed_up(second.summary)
    # It quotes only what the user said since the first.
    assert second.summary.endswith('quoted exactly:\n"message 4"')
    assert {"role": "user", "content": "message 4"} not in setup.llm.requests[-1]["input"]
    assert setup.llm.requests[-1]["input"][-1] == {"role": "user", "content": "message 5"}
    await _close(setup.socket, setup.linked)


async def test_a_gem_in_a_call_is_not_compacted(monkeypatch: pytest.MonkeyPatch) -> None:
    setup = await _a_new_sitting(monkeypatch)
    setup.conversations.gem(str(setup.gem.id)).call = cast("Any", SimpleNamespace())

    with pytest.raises(ConflictError, match="the gem is in a call"):
        await setup.compactions.compact(str(setup.gem.id))
    await _close(setup.socket, setup.linked)


async def test_a_gem_that_remembers_nothing_new_is_not_compacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_clock(monkeypatch, _START)
    setup = await _chat(_said("One."), _summary(), _summary("Again."))
    compactions = await _compactions(setup)
    gem_id = str(setup.gem.id)

    # Nothing said yet.
    with pytest.raises(ConflictError, match="nothing to compact"):
        await compactions.compact(gem_id)
    assert setup.llm.requests == []
    # One message is something to compact; then nothing is, until something new comes.
    socket, linked = await _open(setup.service())
    socket.send(setup.gem.id, "message 1")
    await socket.until("chat.done")
    await compactions.compact(gem_id)
    with pytest.raises(ConflictError, match="nothing to compact"):
        await compactions.compact(gem_id)
    assert len(setup.llm.requests) == 2
    # After a reset, nothing either.
    await setup.conversations.reset(gem_id)
    with pytest.raises(ConflictError, match="nothing to compact"):
        await compactions.compact(gem_id)
    await _close(socket, linked)


@pytest.mark.parametrize("gem_id", [str(ObjectId()), "not-an-id"])
async def test_a_gem_that_is_not_there_is_not_found(gem_id: str) -> None:
    setup = await _chat()

    with pytest.raises(NotFoundError):
        await (await _compactions(setup)).compact(gem_id)


async def test_a_summary_that_fails_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    setup = await _a_new_sitting(monkeypatch)
    failed = SimpleNamespace(
        type="response.failed",
        response=SimpleNamespace(error=SimpleNamespace(message="overloaded")),
    )
    setup.llm.then([failed], _said("Four."))

    with pytest.raises(UpstreamError, match="overloaded"):
        await setup.compactions.compact(str(setup.gem.id))

    log = await _log(setup)
    assert _of(log, "context.compacted") == []
    assert _of(log, "turn.closed")[-1]["outcome"] == "failed"
    setup.socket.send(setup.gem.id, "message 4")
    await setup.socket.until("chat.done", 4)
    assert "summed up" not in json.dumps(setup.llm.requests[-1]["input"])
    await _close(setup.socket, setup.linked)


_REFUSED = SimpleNamespace(type="response.refusal.delta", delta="I won't write that summary.")


@pytest.mark.parametrize(
    ("stream", "why"),
    [
        (_summary(text="I won't write that summary."), "no JSON"),
        (_summary(text=json.dumps({"user_wants": ["To save."]})), "other fields"),
        (_summary(text=json.dumps({key: [] for key in _fields(_POINT)})), "empty summary"),
        ([_REFUSED, _completed()], "declined"),
    ],
)
async def test_a_summary_that_does_not_fit_the_schema_or_is_refused_fails_and_stores_nothing(
    monkeypatch: pytest.MonkeyPatch, stream: list[Any], why: str
) -> None:
    setup = await _a_new_sitting(monkeypatch)
    setup.llm.then(stream)

    async with _routes(setup) as client:
        answer = await client.post(f"/v1/gems/{setup.gem.id}/compact")

    assert answer.status_code == 502
    assert answer.json()["code"] == "upstream_failed"
    assert why in answer.json()["message"]
    log = await _log(setup)
    assert _of(log, "context.compacted") == []
    assert _of(log, "turn.closed")[-1]["outcome"] == "failed"
    await _close(setup.socket, setup.linked)


async def test_only_the_user_s_own_words_are_quoted_not_an_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _a_new_sitting(monkeypatch)
    setup.llm.then(_said("Commits are in."), _summary())
    await _came_in(setup, "push", "a secret commit")
    await setup.socket.until("chat.done", 4)

    compacted = await setup.compactions.compact(str(setup.gem.id))

    quoted = compacted.summary.split("\n\n")[-1]
    assert quoted == 'The user\'s own words, quoted exactly:\n"message 1"\n"message 2"\n"message 3"'
    assert "secret" not in compacted.summary
    await _close(setup.socket, setup.linked)


async def test_a_compaction_that_cannot_be_stored_fails_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _a_new_sitting(monkeypatch)
    setup.llm.then(_summary(), _said("Four."))
    setup.docdb.breaks_on_kind = "context.compacted"

    with pytest.raises(ConnectionError):
        await setup.compactions.compact(str(setup.gem.id))

    setup.docdb.breaks_on_kind = None
    log = await _log(setup)
    assert _of(log, "context.compacted") == []
    assert _of(log, "turn.closed")[-1]["outcome"] == "failed"
    setup.socket.send(setup.gem.id, "message 4")
    await setup.socket.until("chat.done", 4)
    assert "summed up" not in json.dumps(setup.llm.requests[-1]["input"])
    await _close(setup.socket, setup.linked)


async def test_a_compaction_stopped_while_it_is_stored_is_done_once_it_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _a_new_sitting(monkeypatch)
    told: list[str] = []

    async def listener(gem: str) -> None:
        told.append(gem)

    setup.compactions.listen(listener)
    setup.llm.then(_summary())
    setup.docdb.slow = asyncio.Event()
    compacting = asyncio.create_task(setup.compactions.compact(str(setup.gem.id)))
    await _eventually_asked(setup, 4)
    for _ in range(20):
        await asyncio.sleep(0)

    # Stopped (the backend shuts down) while the compaction is written.
    compacting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await compacting
    assert told == []
    setup.docdb.slow.set()
    await _quiet(setup.compactions)
    setup.docdb.slow = None

    log = await _log(setup)
    [compacted] = _of(log, "context.compacted")
    [closed] = [e for e in _of(log, "turn.closed") if e["turn"] == compacted["turn"]]
    assert closed["outcome"] == "finished"
    assert told == [str(setup.gem.id)]
    await _close(setup.socket, setup.linked)


async def test_a_message_sent_during_a_compaction_waits_for_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _a_new_sitting(monkeypatch)
    gate = asyncio.Event()
    setup.llm.then([gate, *_summary()], _said("Four."))
    compacting = asyncio.create_task(setup.compactions.compact(str(setup.gem.id)))
    await _eventually_asked(setup, 4)

    setup.socket.send(setup.gem.id, "message 4")
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(setup.llm.requests) == 4  # the message waits
    gate.set()
    await compacting
    await setup.socket.until("chat.done", 4)

    # It is answered after the compaction, from the summary.
    assert setup.llm.requests[-1]["input"][0] == summed_up(_SUMMARY)
    await _close(setup.socket, setup.linked)


async def test_an_event_that_comes_during_a_compaction_is_sent_whole_after_the_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _a_new_sitting(monkeypatch)
    gate = asyncio.Event()
    setup.llm.then([gate, *_summary()], _said("New commits are in."))
    compacting = asyncio.create_task(setup.compactions.compact(str(setup.gem.id)))
    await _eventually_asked(setup, 4)

    await _came_in(setup, "push")
    gate.set()
    compacted = await compacting
    await setup.socket.until("chat.done", 4)

    # It is told after the summary, which does not hold it.
    told = _of(await _log(setup), "event.received")
    assert told[-1]["seq"] > compacted.through
    sent = setup.llm.requests[-1]["input"]
    assert sent[0] == summed_up(_SUMMARY)
    assert "<event" in json.dumps(sent[1:])
    await _close(setup.socket, setup.linked)


async def test_a_call_started_during_a_compaction_waits_for_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _a_new_sitting(monkeypatch)
    voice = _Voice()
    calling = _CallSetup(setup, voice)
    socket, linked = await _open_link(calling)
    gate = asyncio.Event()
    setup.llm.then([gate, *_summary()])
    compacting = asyncio.create_task(setup.compactions.compact(str(setup.gem.id)))
    await _eventually_asked(setup, 4)

    calling.start(socket)
    for _ in range(20):
        await asyncio.sleep(0)
    # It is not refused, and does not open on the conversation while it is compacted.
    assert not [f for f in socket.frames if f["type"] in ("call.ready", "call.refused")]
    assert voice.made == []
    gate.set()
    compacted = await compacting
    await socket.until("call.ready")

    # The compaction was not cut: it is stored, and its turn finished.
    log = await _log(setup)
    [stored] = _of(log, "context.compacted")
    [closed] = [e for e in _of(log, "turn.closed") if e["turn"] == stored["turn"]]
    assert (stored["through"], closed["outcome"]) == (compacted.through, "finished")
    assert len(voice.made) == 1
    await _close_link(socket, linked)
    await _close(setup.socket, setup.linked)


async def test_a_reply_being_written_is_not_cut_by_a_compaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _a_new_sitting(monkeypatch)
    gate = asyncio.Event()
    setup.llm.then([_delta("Four "), gate, _delta("whole."), _completed()], _summary())
    setup.socket.send(setup.gem.id, "message 4")
    await _eventually_asked(setup, 4)

    compacting = asyncio.create_task(setup.compactions.compact(str(setup.gem.id)))
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(setup.llm.requests) == 4  # it waits for the reply
    gate.set()
    await setup.socket.until("chat.done", 4)
    compacted = await compacting

    assert ("chat.done", "Four whole.") in setup.socket.chat()
    assert compacted.through == await _asked_at(setup)
    await _close(setup.socket, setup.linked)


# ---- only when the user asks


async def test_a_gem_over_the_size_is_never_compacted_unless_the_user_asks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(compaction_ent, "COMPACT_OVER", 10)
    setup = await _a_new_sitting(monkeypatch)
    setup.move(_START + timedelta(minutes=31 + 29))
    await _quiet(setup.compactions)

    assert (await setup.compactions.context(str(setup.gem.id))).compact_from is not None
    assert len(setup.llm.requests) == 3
    log = await _log(setup)
    assert _of(log, "context.compacted") == []
    assert [e["by"] for e in _of(log, "turn.opened")].count("compaction") == 0
    # Nothing is left running for the gem, or holding it.
    assert setup.conversations.find(str(setup.gem.id)) is None
    await _close(setup.socket, setup.linked)


# ---- where the context stands


async def test_the_context_says_its_size_when_the_cache_expires_and_when_to_compact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    move = set_clock(monkeypatch, _START)
    setup = await _chat(_said("One."))
    compactions = await _compactions(setup)
    gem_id = str(setup.gem.id)

    before = await compactions.context(gem_id)
    socket, linked = await _open(setup.service())
    socket.send(setup.gem.id, "message 1")
    await socket.until("chat.done")
    after = await compactions.context(gem_id)
    monkeypatch.setattr(compaction_ent, "COMPACT_OVER", 10)
    over = await compactions.context(gem_id)
    move(_START + timedelta(minutes=31))
    gone = await compactions.context(gem_id)

    # Nothing was sent yet: no cache, and nothing to offer.
    assert (before.cache_until, before.compact_from) == (None, None)
    assert before.tokens > 0
    assert after.tokens > before.tokens
    assert after.cache_until == _START + timedelta(minutes=30)
    assert after.compact_from is None  # under the size
    assert over.compact_from == over.cache_until == _START + timedelta(minutes=30)
    # What each choice costs, at the gem's model's prices.
    assert (after.keep_cost_usd, after.compact_cost_usd) == costs("6 Luna", after.tokens)
    assert (gone.keep_cost_usd, gone.compact_cost_usd) == costs("6 Luna", gone.tokens)
    # The summary request reads no cache: compacting costs the same, warm or cold.
    assert after.compact_cost_usd is not None and after.compact_cost_usd > 0
    with pytest.raises(NotFoundError):
        await compactions.context(str(ObjectId()))
    await _close(socket, linked)


async def test_the_size_starts_from_what_the_latest_request_was_billed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    move = set_clock(monkeypatch, _START)
    setup = await _chat(_summary(text="One reply.", used=True))
    compactions = await _compactions(setup)
    gem_id = str(setup.gem.id)
    socket, linked = await _open(setup.service())
    socket.send(setup.gem.id, "message 1")
    await socket.until("chat.done")

    billed = await compactions.context(gem_id)
    move(_START + timedelta(minutes=31))
    quiet = await compactions.context(gem_id)

    # All of its input, cached or not, then what was remembered since it was sent: the
    # reply.
    assert billed.tokens == 3000 + len("One reply.") // 4
    # Once the cache is gone it still holds: the size is never under while it is offered.
    assert quiet.tokens == billed.tokens
    assert (quiet.cache_until, quiet.compact_from) == (_START + timedelta(minutes=30), None)
    await _close(socket, linked)


async def test_the_size_is_estimated_from_the_text_after_a_compaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _a_new_sitting(monkeypatch)
    gem_id = str(setup.gem.id)
    setup.llm.then(_summary(used=True))

    await setup.compactions.compact(gem_id)

    # The compaction's own request was billed for the conversation it summed up.
    assert (await setup.compactions.context(gem_id)).tokens < 3000
    await _close(setup.socket, setup.linked)


async def test_after_a_compaction_no_cache_is_offered_until_the_next_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(compaction_ent, "COMPACT_OVER", 10)
    setup = await _a_new_sitting(monkeypatch)
    gem_id = str(setup.gem.id)
    failed = SimpleNamespace(
        type="response.failed",
        response=SimpleNamespace(error=SimpleNamespace(message="overloaded")),
    )
    setup.llm.then([failed], _summary(), _said("Four."))

    # A summary that failed changed nothing: the cache is still offered.
    with pytest.raises(UpstreamError):
        await setup.compactions.compact(gem_id)
    failing = await setup.compactions.context(gem_id)
    await setup.compactions.compact(gem_id)
    compacted = await setup.compactions.context(gem_id)
    setup.socket.send(setup.gem.id, "message 4")
    await setup.socket.until("chat.done", 4)
    after = await setup.compactions.context(gem_id)

    assert failing.cache_until is not None
    assert failing.compact_from == failing.cache_until
    # The compaction's own request cached nothing, and no request after it reads the
    # cache the gem had: nothing is offered until the next request.
    assert (compacted.cache_until, compacted.compact_from) == (None, None)
    assert compacted.compact_cost_usd == costs("6 Luna", compacted.tokens)[1]
    assert after.cache_until is not None
    assert after.compact_from == after.cache_until
    await _close(setup.socket, setup.linked)


async def test_the_size_counts_what_is_sent_summed_up_and_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _a_new_sitting(monkeypatch)
    gem_id = str(setup.gem.id)
    whole = await setup.compactions.context(gem_id)
    setup.llm.then(_summary())

    await setup.compactions.compact(gem_id)

    assert (await setup.compactions.context(gem_id)).tokens < whole.tokens + len(_SUMMARY)
    await _close(setup.socket, setup.linked)


async def test_whoever_listens_is_told_after_a_reply_a_compaction_and_a_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _a_new_sitting(monkeypatch)
    told: list[str] = []

    async def listener(gem: str) -> None:
        told.append(gem)

    setup.compactions.listen(listener)
    setup.llm.then(_said("Four."), _summary())
    setup.socket.send(setup.gem.id, "message 4")
    await setup.socket.until("chat.done", 4)
    await _quiet(setup.compactions)
    await setup.compactions.compact(str(setup.gem.id))
    await _quiet(setup.compactions)
    await setup.conversations.reset(str(setup.gem.id))
    await _quiet(setup.compactions)
    setup.compactions.ignore(listener)

    assert told == [str(setup.gem.id)] * 3
    await _close(setup.socket, setup.linked)


# ---- the routes


@asynccontextmanager
async def _routes(setup: Any) -> AsyncIterator[httpx.AsyncClient]:
    """A client of the app, whose gem routes reach `setup`'s gems."""
    app.dependency_overrides[get_gems_service_v1] = lambda: GemsServiceV1Class(
        setup.gems, setup.conversations, compactions=setup.compactions
    )
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


async def test_the_compact_route_answers_with_the_summary_and_its_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _a_new_sitting(monkeypatch)
    setup.llm.then(_summary())

    async with _routes(setup) as client:
        answer = await client.post(f"/v1/gems/{setup.gem.id}/compact")
        again = await client.post(f"/v1/gems/{setup.gem.id}/compact")

    assert answer.status_code == 200
    assert answer.json() == {"summary": _SUMMARY, "through": await _asked_at(setup)}
    # Everything is summed up now: nothing is left to compact.
    assert (again.status_code, again.json()) == (
        409,
        {"code": "conflict", "message": "nothing to compact"},
    )
    await _close(setup.socket, setup.linked)


async def test_the_compact_route_refuses_a_gem_in_a_call_and_one_that_is_not_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _a_new_sitting(monkeypatch)
    setup.conversations.gem(str(setup.gem.id)).call = cast("Any", SimpleNamespace())

    async with _routes(setup) as client:
        in_call = await client.post(f"/v1/gems/{setup.gem.id}/compact")
        missing = await client.post(f"/v1/gems/{ObjectId()}/compact")
        no_context = await client.get(f"/v1/gems/{ObjectId()}/context")

    assert (in_call.status_code, in_call.json()) == (
        409,
        {"code": "conflict", "message": "the gem is in a call"},
    )
    assert missing.status_code == no_context.status_code == 404
    await _close(setup.socket, setup.linked)


async def test_the_compact_route_says_why_a_summary_failed_and_stores_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _a_new_sitting(monkeypatch)
    failed = SimpleNamespace(
        type="response.failed",
        response=SimpleNamespace(error=SimpleNamespace(message="overloaded")),
    )
    setup.llm.then([failed])

    async with _routes(setup) as client:
        answer = await client.post(f"/v1/gems/{setup.gem.id}/compact")

    assert answer.status_code == 502
    assert answer.json()["code"] == "upstream_failed"
    assert "overloaded" in answer.json()["message"]
    assert _of(await _log(setup), "context.compacted") == []
    await _close(setup.socket, setup.linked)


async def test_the_context_route_says_where_the_context_stands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _a_new_sitting(monkeypatch)
    monkeypatch.setattr(compaction_ent, "COMPACT_OVER", 10)

    async with _routes(setup) as client:
        answer = await client.get(f"/v1/gems/{setup.gem.id}/context")

    body = answer.json()
    assert answer.status_code == 200
    assert set(body) == {
        "tokens",
        "cache_until",
        "compact_from",
        "keep_cost_usd",
        "compact_cost_usd",
    }
    assert body["tokens"] > 10
    assert datetime.fromisoformat(body["cache_until"]) == _START + timedelta(minutes=61)
    assert body["compact_from"] == body["cache_until"]
    assert body["keep_cost_usd"] > 0
    assert body["compact_cost_usd"] > 0
    # With no price known for the gem's model, neither cost is known: null, never zero.
    monkeypatch.delitem(prices._PRICES, "6 Luna")
    async with _routes(setup) as client:
        unpriced = (await client.get(f"/v1/gems/{setup.gem.id}/context")).json()
    assert (unpriced["keep_cost_usd"], unpriced["compact_cost_usd"]) == (None, None)
    await _close(setup.socket, setup.linked)


# ---- the link


async def test_every_socket_is_told_when_a_gems_context_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _a_new_sitting(monkeypatch)
    watching, linked = await _open(
        LinkServiceV1Class(setup.chat, setup.service().call, compactions=setup.compactions)
    )
    setup.llm.then(_said("Four."), _summary())
    changed = {"type": "context.changed", "gem": str(setup.gem.id)}

    # After a reply, from another socket.
    setup.socket.send(setup.gem.id, "message 4")
    await setup.socket.until("chat.done", 4)
    await watching.until("context.changed")
    # After a compaction, and after a reset.
    await setup.compactions.compact(str(setup.gem.id))
    await watching.until("context.changed", 2)
    await setup.conversations.reset(str(setup.gem.id))
    await watching.until("context.changed", 3)

    assert [f for f in watching.frames if f["type"] == "context.changed"] == [changed] * 3
    # Nothing else of the gem's reply reached the socket that did not write to it.
    assert watching.chat() == []
    await _close(watching, linked)
    await _close(setup.socket, setup.linked)
