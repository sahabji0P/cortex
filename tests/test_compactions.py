"""Compacting a gem's conversation: when the user asks, and by itself before its cache expires.

The clock is set (`set_clock`), and the gems use an OpenAI model, whose cache is held to
last 30 minutes. A conversation has something to compact once it has started cold: the
latest sitting is kept whole, and what came before it is summed up. The size a gem
compacts itself past is lowered, so a short conversation is over it.
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
from app.entities import compaction_ent
from app.entities.compaction import ASK, SUMMARY_TOKENS, Compacted, summed_up
from app.entities.compaction_ent import CompactionEntityClass
from app.entities.darbar_ent import DarbarEntityClass
from app.main import app
from app.routers.gems.service import GemsServiceV1Class, get_gems_service_v1
from app.routers.link.service import LinkServiceV1Class
from tests.test_chat import _chat, _close, _eventually_asked, _open, _said
from tests.test_one_conversation import _hang_up, _said_and_heard, _spoken, _world
from tests.test_voice_conversation import _completed, _delta
from tests.time_lines import TIME_LINE, set_clock

_START = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)
_SUMMARY = "What the user wants: to save. Decisions: none."


class _Sleeper:
    """How a compaction set to run waits: each wait is written down, and waits for the test."""

    def __init__(self) -> None:
        self.waits: list[float] = []
        self.stopped = 0
        self.go = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)
        try:
            await self.go.wait()
        except asyncio.CancelledError:
            self.stopped += 1
            raise


def _summary(text: str = _SUMMARY, *, used: bool = False) -> list[Any]:
    done = _completed()
    if used:
        done.response.usage = SimpleNamespace(
            input_tokens=3000,
            input_tokens_details=SimpleNamespace(cached_tokens=2800, cache_write_tokens=0),
            output_tokens=120,
            output_tokens_details=SimpleNamespace(reasoning_tokens=0),
        )
    return [_delta(text), done]


async def _compactions(setup: Any, sleep: Any = None) -> CompactionEntityClass:
    adapter = cast("Any", setup.store)._docdb
    return CompactionEntityClass(
        gems=setup.gems,
        darbar=DarbarEntityClass(adapter),
        store=setup.store,
        conversations=setup.conversations,
        chat=setup.chat,
        sleep=_Sleeper() if sleep is None else sleep,
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
    sleep = gem.pop("sleep", None)
    setup = await _chat(_said("One."), _said("Two."), _said("Three."), **gem)
    setup.compactions = await _compactions(setup, sleep)
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


async def _sitting_starts(setup: Any) -> int:
    """The number of the first thing remembered after the latest cold start."""
    log = await _log(setup)
    cold = _of(log, "cache.cold")[-1]["seq"]
    return next(e["seq"] for e in log if e["seq"] > cold and e["kind"] == "note.written")


# ---- the user's compaction


async def test_a_compaction_sums_up_what_came_before_the_latest_sitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _a_new_sitting(monkeypatch)
    setup.llm.then(_summary(used=True), _said("Four."))

    compacted = await setup.compactions.compact(str(setup.gem.id))

    assert compacted == Compacted(_SUMMARY, await _sitting_starts(setup))
    # The summary request was sent what the next reply would be, then the ask, with
    # tool use off.
    summary_request = setup.llm.requests[-1]
    assert summary_request["input"][:-1] == setup.llm.requests[-2]["input"] + [
        {"role": "assistant", "content": "Three."}
    ]
    assert summary_request["input"][-1] == {"role": "developer", "content": ASK}
    assert summary_request["tool_choice"] == "none"
    assert summary_request["tools"] == setup.llm.requests[-2]["tools"]
    # The next reply is sent the summary, then the latest sitting whole, from the line
    # that gave the time with its first message.
    setup.move(_START + timedelta(minutes=33))
    setup.socket.send(setup.gem.id, "message 4")
    await setup.socket.until("chat.done", 4)
    assert setup.llm.requests[-1]["input"] == [
        summed_up(_SUMMARY),
        TIME_LINE,
        {"role": "user", "content": "message 3"},
        {"role": "assistant", "content": "Three."},
        {"role": "user", "content": "message 4"},
    ]
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
        "input": 200,
        "cache_read": 2800,
        "cache_write": 0,
        "output": 120,
        "thinking": 0,
    }
    assert done["cost_usd"] is not None and done["cost_usd"] > 0
    assert done["text"] == _SUMMARY
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
    assert (options["use_tools"], options["max_tokens"]) == (False, SUMMARY_TOKENS)
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
    assert read["items"][-1] == {"role": "developer", "content": ASK}
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

    assert compacted.summary.startswith(_SUMMARY + "\n\n")
    assert compacted.summary.splitlines()[-2:] == [
        "- INV-77812",
        "- https://pay.example.com/r/AB12CD",
    ]
    await _close(socket, linked)


async def test_a_cold_gem_is_compacted_from_its_latest_sitting_and_starts_cold_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _a_new_sitting(monkeypatch)
    through = await _sitting_starts(setup)
    setup.move(_START + timedelta(minutes=31 + 31))
    setup.llm.then(_summary())
    setup.llm.starts_cold()

    compacted = await setup.compactions.compact(str(setup.gem.id))

    log = await _log(setup)
    opened = _of(log, "turn.opened")[-1]
    colds = _of(log, "cache.cold")
    assert compacted.through == through
    # The compaction's request is the gem's next: it started cold, first.
    assert len(colds) == 2
    assert colds[-1]["seq"] < opened["seq"]
    [sent] = [e for e in _of(log, "request.sent") if e["turn"] == opened["turn"]]
    service = GemsServiceV1Class(setup.gems, setup.conversations)
    assert (await service.get_log_sent(str(setup.gem.id), sent["seq"]))["exact"]
    await _close(setup.socket, setup.linked)


async def test_a_cold_gem_whose_summary_failed_is_compacted_the_same_when_asked_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _a_new_sitting(monkeypatch)
    through = await _sitting_starts(setup)
    setup.move(_START + timedelta(minutes=31 + 31))
    failed = SimpleNamespace(
        type="response.failed",
        response=SimpleNamespace(error=SimpleNamespace(message="overloaded")),
    )
    setup.llm.then([failed], _summary())
    setup.llm.starts_cold()

    with pytest.raises(UpstreamError):
        await setup.compactions.compact(str(setup.gem.id))
    # The cold start it wrote stays: nothing remembered follows it, so the sitting is
    # still the one before it.
    compacted = await setup.compactions.compact(str(setup.gem.id))

    assert compacted.through == through
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
    assert summary_request["input"][0] == summed_up("First.")
    assert setup.llm.requests[-1]["input"][0] == summed_up("Second.")
    assert {"role": "user", "content": "message 3"} not in setup.llm.requests[-1]["input"]
    assert {"role": "user", "content": "message 4"} in setup.llm.requests[-1]["input"]
    await _close(setup.socket, setup.linked)


async def test_a_gem_in_a_call_is_not_compacted(monkeypatch: pytest.MonkeyPatch) -> None:
    setup = await _a_new_sitting(monkeypatch)
    setup.conversations.gem(str(setup.gem.id)).call = cast("Any", SimpleNamespace())

    with pytest.raises(ConflictError, match="the gem is in a call"):
        await setup.compactions.compact(str(setup.gem.id))
    await _close(setup.socket, setup.linked)


async def test_a_conversation_with_nothing_before_its_latest_sitting_is_not_compacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_clock(monkeypatch, _START)
    setup = await _chat(_said("One."))
    compactions = await _compactions(setup)
    socket, linked = await _open(setup.service())
    socket.send(setup.gem.id, "message 1")
    await socket.until("chat.done")
    asked = len(setup.llm.requests)

    with pytest.raises(ConflictError, match="nothing to compact"):
        await compactions.compact(str(setup.gem.id))

    assert len(setup.llm.requests) == asked
    assert _of(await _log(setup), "turn.opened")[-1]["by"] == "user"
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
    assert compacted.through == await _sitting_starts(setup)
    await _close(setup.socket, setup.linked)


# ---- before the cache expires


@pytest.fixture
def _small(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gem compacts itself past a size any conversation here is over."""
    monkeypatch.setattr(compaction_ent, "COMPACT_OVER", 10)


@pytest.mark.usefixtures("_small")
async def test_a_compaction_is_set_to_run_two_minutes_before_the_cache_expires_and_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeper = _Sleeper()
    setup = await _a_new_sitting(monkeypatch, sleep=sleeper)
    setup.llm.then(_summary())

    # Set after the last reply: the messages before took the gem before it was set.
    assert len(sleeper.waits) == 1
    # The cache lasts 30 minutes from the last request; it runs 2 minutes before.
    assert sleeper.waits[-1] == pytest.approx(28 * 60)
    setup.move(_START + timedelta(minutes=31 + 28))
    sleeper.go.set()
    await _quiet(setup.compactions)

    [compacted] = _of(await _log(setup), "context.compacted")
    assert (compacted["by"], compacted["through"]) == ("auto", await _sitting_starts(setup))
    assert setup.llm.requests[-1]["tool_choice"] == "none"
    # Nothing was said to the user.
    assert [type_ for type_, _ in setup.socket.chat()].count("chat.done") == 3
    # It is not set to run again after itself.
    assert len(sleeper.waits) == 1
    await _close(setup.socket, setup.linked)


@pytest.mark.usefixtures("_small")
async def test_a_compaction_set_to_run_stops_before_asking_the_model_if_the_settings_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeper = _Sleeper()
    setup = await _a_new_sitting(monkeypatch, sleep=sleeper)
    await setup.gems.update(str(setup.gem.id), {"instructions": "You are terse."})

    setup.move(_START + timedelta(minutes=31 + 28))
    sleeper.go.set()
    await _quiet(setup.compactions)

    # Its summary would read no cache: the model is not asked, and nothing is compacted.
    assert len(setup.llm.requests) == 3
    log = await _log(setup)
    assert _of(log, "cache.cold")[-1]["reason"] == "settings"
    assert [e["by"] for e in _of(log, "turn.opened")].count("compaction") == 0
    assert _of(log, "context.compacted") == []
    assert setup.conversations.gem(str(setup.gem.id)).writer is None
    # The next reply starts from the cold start already stored: it is not cold again.
    setup.llm.then(_said("Four."))
    setup.socket.send(setup.gem.id, "message 4")
    await setup.socket.until("chat.done", 4)
    assert len(_of(await _log(setup), "cache.cold")) == len(_of(log, "cache.cold"))
    await _close(setup.socket, setup.linked)


@pytest.mark.usefixtures("_small")
async def test_a_compaction_set_to_run_is_stopped_by_a_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeper = _Sleeper()
    setup = await _a_new_sitting(monkeypatch, sleep=sleeper)
    setup.llm.then(_said("Four."))

    setup.socket.send(setup.gem.id, "message 4")
    await setup.socket.until("chat.done", 4)
    await _quiet(setup.compactions)

    assert sleeper.stopped == 1
    assert len(sleeper.waits) == 2  # set again after the reply
    await _close(setup.socket, setup.linked)


@pytest.mark.usefixtures("_small")
async def test_a_compaction_set_to_run_is_stopped_by_a_call_and_never_runs_in_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeper = _Sleeper()
    setup = await _a_new_sitting(monkeypatch, sleep=sleeper)
    kept = setup.conversations.gem(str(setup.gem.id))

    kept.call = cast("Any", SimpleNamespace())
    sleeper.go.set()
    await _quiet(setup.compactions)

    assert sleeper.stopped == 1
    assert kept.auto is None
    assert _of(await _log(setup), "context.compacted") == []
    await _close(setup.socket, setup.linked)


@pytest.mark.usefixtures("_small")
async def test_a_compaction_is_set_to_run_after_a_call_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_clock(monkeypatch, _START)
    world = await _world(_spoken("Hello."))
    sleeper = _Sleeper()
    compactions = CompactionEntityClass(
        gems=world.chat._gems,
        darbar=DarbarEntityClass(world.store._docdb),
        store=world.store,
        conversations=world.conversations,
        chat=world.chat,
        sleep=sleeper,
    )

    call, f, keeping = await world.call()
    await _said_and_heard(f, "hello")
    await _quiet(compactions)
    assert sleeper.waits == []  # never while the call holds the gem
    await _hang_up(call, keeping)
    await _quiet(compactions)

    assert sleeper.waits == [pytest.approx(28 * 60)]
    sleeper.go.set()
    await _quiet(compactions)


@pytest.mark.usefixtures("_small")
@pytest.mark.parametrize("setting", ["on_return", "never"])
async def test_a_gem_not_set_to_compact_before_expiry_is_never_compacted_by_itself(
    monkeypatch: pytest.MonkeyPatch, setting: str
) -> None:
    sleeper = _Sleeper()
    setup = await _a_new_sitting(monkeypatch, sleep=sleeper, compaction=setting)

    assert sleeper.waits == []
    await _close(setup.socket, setup.linked)


async def test_a_gem_under_the_size_is_not_compacted_by_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeper = _Sleeper()
    setup = await _a_new_sitting(monkeypatch, sleep=sleeper)

    assert sleeper.waits == []
    await _close(setup.socket, setup.linked)


@pytest.mark.usefixtures("_small")
async def test_a_compaction_that_runs_is_cut_by_a_message_and_stores_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeper = _Sleeper()
    setup = await _a_new_sitting(monkeypatch, sleep=sleeper)
    gate = asyncio.Event()
    setup.llm.then([_delta("What the"), gate, *_summary()], _said("Four."))
    setup.move(_START + timedelta(minutes=31 + 28))
    sleeper.go.set()
    await _eventually_asked(setup, 4)

    setup.socket.send(setup.gem.id, "message 4")
    await setup.socket.until("chat.done", 4)

    log = await _log(setup)
    assert _of(log, "context.compacted") == []
    assert [e["outcome"] for e in _of(log, "request.done")][-2] == "cut"
    assert setup.llm.requests[-1]["input"][0] != summed_up(_SUMMARY)
    await _close(setup.socket, setup.linked)


# ---- where the context stands


async def test_the_context_says_its_size_when_the_cache_expires_and_when_to_compact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_clock(monkeypatch, _START)
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
    await setup.gems.update(gem_id, {"compaction": "never"})
    never = await compactions.context(gem_id)

    # Nothing was sent yet: no cache, and nothing to offer.
    assert (before.cache_until, before.compact_from, before.compaction) == (
        None,
        None,
        "before_expiry",
    )
    assert before.tokens > 0
    assert after.tokens > before.tokens
    assert after.cache_until == _START + timedelta(minutes=30)
    assert after.compact_from is None  # under the size
    assert over.compact_from == over.cache_until == _START + timedelta(minutes=30)
    assert (never.compact_from, never.compaction) == (None, "never")
    with pytest.raises(NotFoundError):
        await compactions.context(str(ObjectId()))
    await _close(socket, linked)


async def test_the_size_starts_from_what_the_latest_request_was_billed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    move = set_clock(monkeypatch, _START)
    setup = await _chat(_summary("One reply.", used=True))
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


@pytest.mark.usefixtures("_small")
async def test_after_a_compaction_no_cache_is_offered_until_the_next_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = await _a_new_sitting(monkeypatch, compaction="on_return")
    gem_id = str(setup.gem.id)
    failed = SimpleNamespace(
        type="response.failed",
        response=SimpleNamespace(error=SimpleNamespace(message="overloaded")),
    )
    setup.llm.then([failed], _summary(), _said("Four."))

    # A summary that failed changed nothing: its request read the cache, and kept it.
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
    # What the compaction's own request cached is read by no request after it.
    assert (compacted.cache_until, compacted.compact_from) == (None, None)
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
    assert answer.json() == {"summary": _SUMMARY, "through": await _sitting_starts(setup)}
    # What came before is summed up now: nothing is left to compact.
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
    assert set(body) == {"tokens", "cache_until", "compact_from", "compaction"}
    assert body["tokens"] > 10
    assert datetime.fromisoformat(body["cache_until"]) == _START + timedelta(minutes=61)
    assert body["compact_from"] == body["cache_until"]
    assert body["compaction"] == "before_expiry"
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
