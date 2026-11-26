"""Watches: what a gem watches for, kept, run and removed.

The database, the event sink, the connections' MCP events and the clock are stand-ins,
so a timer's hours pass at once and nothing reaches the internet.
"""

import asyncio
import hashlib
import hmac
import json
from collections.abc import AsyncGenerator, Callable, Mapping
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from bson import ObjectId

from app.config import settings
from app.core.errors import InvalidError, NotFoundError
from app.entities.conversation_store import ConversationStoreClass
from app.entities.gem_ent import GemConfig, GemEntityClass
from app.entities.gem_event import (
    EVENT_TEXT_LIMIT,
    GemEvent,
    McpEventKind,
    McpWatch,
    OnMcpCursor,
    OnMcpDelivery,
    OnMcpEvent,
)
from app.entities.gem_log import LogEntityClass
from app.entities.tools import Toolbox
from app.entities.watch_ent import Watch, WatchEntityClass
from app.main import app
from app.routers.hooks.service import HooksServiceV1Class, get_hooks_service_v1
from app.routers.link.service import LinkServiceV1Class
from app.routers.watches.service import WatchesServiceV1Class, get_watches_service_v1
from tests.test_chat import (
    _chat,  # pyright: ignore[reportPrivateUsage]
    _close,  # pyright: ignore[reportPrivateUsage]
    _DocDB,  # pyright: ignore[reportPrivateUsage]
    _docdb,  # pyright: ignore[reportPrivateUsage]
    _open,  # pyright: ignore[reportPrivateUsage]
)

_PUBLIC = "https://hooks.example.com"
_START = datetime(2026, 11, 26, 9, 0, tzinfo=UTC)


async def _until(done: Callable[[], bool]) -> None:
    """Let the tasks run until `done` holds."""
    for _ in range(10_000):
        if done():
            return
        await asyncio.sleep(0)
    raise AssertionError("it never came to hold")


class _Clock:
    """A clock whose time moves only when the test moves it."""

    def __init__(self) -> None:
        self.at = _START
        self._sleepers: list[tuple[datetime, asyncio.Future[None]]] = []

    def now(self) -> datetime:
        return self.at

    async def sleep(self, seconds: float) -> None:
        waking: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.at + timedelta(seconds=seconds), waking))
        try:
            await waking
        finally:
            self._sleepers = [one for one in self._sleepers if one[1] is not waking]

    @property
    def sleeping(self) -> int:
        return len(self._sleepers)

    async def advance(self, **delta: float) -> None:
        """Move the time on, and wake whoever sleeps until then."""
        self.at += timedelta(**delta)
        for until, waking in list(self._sleepers):
            if until <= self.at and not waking.done():
                waking.set_result(None)
        for _ in range(50):
            await asyncio.sleep(0)


# What a watch tells its gem of itself, when its state changes.
_CHANGES = ("watch.failing", "watch.paused", "watch.recovered")


class _Sink:
    """The event sink: it keeps what it is handed, or fails while `fails` is set."""

    def __init__(self) -> None:
        self.events: list[GemEvent] = []
        self.fails: Exception | None = None

    async def arrived(self, event: GemEvent) -> None:
        if self.fails is not None:
            raise self.fails
        self.events.append(event)

    @property
    def caught(self) -> list[GemEvent]:
        """The events the watches caught."""
        return [event for event in self.events if event.name not in _CHANGES]

    @property
    def changes(self) -> list[tuple[str, str]]:
        """What the watches told of themselves: each change's name and words."""
        return [(event.name, event.data) for event in self.events if event.name in _CHANGES]


_OPENED = McpEventKind(
    connection_id="6afdb62efdd277b795ce9455",
    connection_key="issue_tracker",
    name="issue.opened",
    description="An issue was opened.",
    delivery=["push"],
    input_schema={"type": "object", "properties": {"repo": {"type": "string"}}},
)


class _McpEvents:
    """The connections' MCP events: each watch runs until it is cancelled, or fails."""

    def __init__(self) -> None:
        self.open: dict[ObjectId, list[McpEventKind]] = {}
        # Each watch run, with the address its events may be posted to.
        self.runs: list[tuple[McpWatch, str | None]] = []
        self.running: dict[str, OnMcpEvent] = {}
        # What each watch running is told its cursor with, when it moves with no event.
        self.cursors: dict[str, OnMcpCursor] = {}
        # What each watch running is told the delivery it starts on with.
        self.deliveries: dict[str, OnMcpDelivery] = {}
        # What each watch running waits on: it fails when this does.
        self.ending: dict[str, asyncio.Future[None]] = {}
        self.stopped: list[str] = []
        self.fails: Exception | None = None
        # Whether a post is one the server signed, and what each was posted with.
        self.signed = True
        self.posts: list[tuple[McpWatch, dict[str, str], bytes]] = []

    async def kinds_for(self, gem_id: ObjectId) -> list[McpEventKind]:
        return self.open.get(gem_id, [])

    async def watch(
        self,
        watch: McpWatch,
        on_event: OnMcpEvent,
        hook_url: str | None,
        on_cursor: OnMcpCursor | None = None,
        on_delivery: OnMcpDelivery | None = None,
    ) -> None:
        self.runs.append((watch, hook_url))
        if self.fails is not None:
            raise self.fails
        self.running[watch.watch_id] = on_event
        if on_cursor is not None:
            self.cursors[watch.watch_id] = on_cursor
        if on_delivery is not None:
            self.deliveries[watch.watch_id] = on_delivery
        ending = self.ending[watch.watch_id] = asyncio.get_running_loop().create_future()
        try:
            await ending
        finally:
            del self.running[watch.watch_id]
            self.cursors.pop(watch.watch_id, None)
            self.deliveries.pop(watch.watch_id, None)
            self.ending.pop(watch.watch_id, None)
            self.stopped.append(watch.watch_id)

    def fail(self, watch_id: str, failure: Exception) -> None:
        """Make a watch that runs fail now, as when its server stops answering."""
        self.ending[watch_id].set_exception(failure)

    async def posted(
        self, watch: McpWatch, headers: Mapping[str, str], body: bytes, on_event: OnMcpEvent
    ) -> bool:
        self.posts.append((watch, dict(headers), body))
        if not self.signed:
            return False
        await on_event("issue.opened", body.decode(), "p1", None, "c9")
        return True


class _Setup(SimpleNamespace):
    docdb: _DocDB
    adapter: Any
    gems: GemEntityClass
    log: LogEntityClass
    clock: _Clock
    sink: _Sink
    mcp: _McpEvents
    watches: WatchEntityClass
    one: GemConfig
    two: GemConfig
    told: list[tuple[str, str]]

    def entity(self) -> WatchEntityClass:
        """Another entity on the same database, as after a restart."""
        return WatchEntityClass(
            docdb=self.adapter,
            gems=self.gems,
            log=self.log,
            sink=self.sink,
            mcp_events=self.mcp,
            now=self.clock.now,
            sleep=self.clock.sleep,
        )

    def watch(self, made: Watch) -> Watch:
        """The watch as it is stored now."""
        return Watch.model_validate(next(d for d in self.stored() if d["_id"] == made.id))

    def stored(self) -> list[dict[str, Any]]:
        return self.docdb.collections.get("Watches", [])

    async def logged(self, gem: GemConfig) -> list[tuple[str, dict[str, Any]]]:
        await self.log.written(gem.id)
        return [
            (d["kind"], d["data"])
            for d in self.docdb.collections.get("GemLog", [])
            if d["gem_id"] == gem.id
        ]


@pytest.fixture
async def setup(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[_Setup]:
    monkeypatch.setattr(settings, "public_url", _PUBLIC)
    docdb, adapter = _docdb()
    gems = GemEntityClass(adapter)
    made = _Setup(
        docdb=docdb,
        adapter=adapter,
        gems=gems,
        log=LogEntityClass(ConversationStoreClass(adapter)),
        clock=_Clock(),
        sink=_Sink(),
        mcp=_McpEvents(),
        one=await gems.create(name="Gem One", model="6 Luna"),
        two=await gems.create(name="Gem Two", model="6 Luna"),
        told=[],
    )
    made.watches = made.entity()

    async def listener(gem: str, watch: str) -> None:
        made.told.append((gem, watch))

    made.watches.listen(listener)
    await made.watches.start()
    yield made
    await made.watches.close()


# ---- what is kept


async def test_a_webhook_watch_is_kept_with_an_address_of_its_own(setup: _Setup) -> None:
    made = await setup.watches.add_webhook(setup.one.id, "Review each new pull request.")
    other = await setup.watches.add_webhook(setup.one.id, "Say when a build fails.")

    assert made.token is not None and len(made.token) >= 43
    assert made.token != other.token
    assert made.hook_url == f"{_PUBLIC}/v1/hooks/{made.token}"
    [stored, _] = setup.stored()
    assert Watch.model_validate(stored) == made
    assert (made.state, made.reason, made.events, made.last_event_at) == ("active", None, 0, None)


async def test_a_webhook_watch_is_refused_with_no_public_address(
    setup: _Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "public_url", "")

    with pytest.raises(InvalidError, match="no public address"):
        await setup.watches.add_webhook(setup.one.id, "Review each new pull request.")
    assert setup.stored() == []


async def test_a_gem_s_watches_are_listed_newest_first(setup: _Setup) -> None:
    first = await setup.watches.add_webhook(setup.one.id, "first")
    await setup.watches.add_webhook(setup.two.id, "not hers")
    second = await setup.watches.add_webhook(setup.one.id, "second")

    listed = await setup.watches.list_for(setup.one.id)

    assert [watch.id for watch in listed] == [second.id, first.id]


# ---- removing


async def test_a_watch_removed_goes_is_written_down_and_told(setup: _Setup) -> None:
    made = await setup.watches.add_webhook(setup.one.id, "Review each new pull request.")

    await setup.watches.remove(setup.one.id, str(made.id), by="gem")

    assert setup.stored() == []
    assert await setup.logged(setup.one) == [
        (
            "watch.added",
            {"watch": str(made.id), "source": "webhook", "about": "Review each new pull request."},
        ),
        ("watch.removed", {"watch": str(made.id), "by": "gem"}),
    ]
    await _until(lambda: len(setup.told) == 2)
    assert setup.told == [(str(setup.one.id), str(made.id))] * 2


async def test_a_watch_of_another_gem_or_none_is_not_found(setup: _Setup) -> None:
    made = await setup.watches.add_webhook(setup.one.id, "first")

    for gem, watch in [(setup.two.id, str(made.id)), (setup.one.id, "nope"), (setup.one.id, "")]:
        with pytest.raises(NotFoundError):
            await setup.watches.remove(gem, watch, by="user")
    assert len(setup.stored()) == 1


async def test_end_for_removes_every_watch_of_the_gem_and_no_other(setup: _Setup) -> None:
    first = await setup.watches.add_webhook(setup.one.id, "first")
    second = await setup.watches.add_webhook(setup.one.id, "second")
    kept = await setup.watches.add_webhook(setup.two.id, "kept")

    await setup.watches.end_for(setup.one.id)

    assert [d["_id"] for d in setup.stored()] == [kept.id]
    removed = [data for kind, data in await setup.logged(setup.one) if kind == "watch.removed"]
    assert removed == [
        {"watch": str(second.id), "by": "user"},
        {"watch": str(first.id), "by": "user"},
    ]


async def test_a_deleted_gem_s_watches_go_with_it(setup: _Setup) -> None:
    await setup.watches.add_webhook(setup.one.id, "first")
    kept = await setup.watches.add_webhook(setup.two.id, "kept")

    await setup.gems.delete(str(setup.one.id))
    await _until(lambda: len(setup.stored()) == 1)

    assert [d["_id"] for d in setup.stored()] == [kept.id]


async def test_the_token_is_not_in_the_log(setup: _Setup) -> None:
    made = await setup.watches.add_webhook(setup.one.id, "Review each new pull request.")
    assert made.token is not None

    assert made.token not in repr(await setup.logged(setup.one))


async def test_a_watch_is_not_listed_for_an_unknown_gem(setup: _Setup) -> None:
    await setup.watches.add_webhook(setup.one.id, "first")

    assert await setup.watches.list_for(ObjectId()) == []


# ---- timers


async def test_a_timer_goes_off_on_its_rhythm(setup: _Setup) -> None:
    made = await setup.watches.add_timer(setup.one.id, "Check the inbox.", 5)
    assert made.due_at == _START + timedelta(minutes=5)

    await setup.clock.advance(minutes=4, seconds=59)
    assert setup.sink.events == []
    await setup.clock.advance(seconds=1)
    await setup.clock.advance(minutes=5)

    assert setup.sink.events == [
        GemEvent(
            gem_id=setup.one.id,
            watch_id=str(made.id),
            source="timer",
            name="timer",
            about="Check the inbox.",
            data="",
            at=_START + timedelta(minutes=minutes),
            event_id=f"timer:{(_START + timedelta(minutes=minutes)).isoformat()}",
        )
        for minutes in (5, 10)
    ]
    kept = setup.watch(made)
    assert (kept.events, kept.last_event_at) == (2, _START + timedelta(minutes=10))
    assert kept.due_at == _START + timedelta(minutes=15)


async def test_a_timer_that_came_due_while_down_goes_off_once_then_keeps_its_rhythm(
    setup: _Setup,
) -> None:
    made = await setup.watches.add_timer(setup.one.id, "Check the inbox.", 5)
    await setup.watches.close()

    # Down from 9:00 to 9:12: it was due at 9:05 and 9:10.
    await setup.clock.advance(minutes=12)
    assert setup.sink.events == []
    again = setup.entity()
    await again.start()
    await setup.clock.advance()

    assert [event.at for event in setup.sink.events] == [_START + timedelta(minutes=12)]
    assert setup.watch(made).due_at == _START + timedelta(minutes=15)
    await setup.clock.advance(minutes=3)
    assert len(setup.sink.events) == 2
    await again.close()


async def test_a_timer_under_the_least_interval_is_refused(setup: _Setup) -> None:
    with pytest.raises(InvalidError, match="at most every 5 minutes"):
        await setup.watches.add_timer(setup.one.id, "Check the inbox.", 4)
    assert setup.stored() == []


async def test_a_timer_whose_event_cannot_be_kept_fails_and_comes_back(setup: _Setup) -> None:
    made = await setup.watches.add_timer(setup.one.id, "Check the inbox.", 5)
    await _until(lambda: len(setup.told) == 1)
    setup.sink.fails = ConnectionError("the database went away")

    await setup.clock.advance(minutes=5)
    kept = setup.watch(made)
    assert (kept.state, kept.reason) == ("failing", "the database went away")
    assert setup.told[-1] == (str(setup.one.id), str(made.id))
    # Tried again after 10 seconds, then 20: still failing, and told only once.
    await setup.clock.advance(seconds=10)
    assert setup.watch(made).state == "failing"
    told = len(setup.told)
    setup.sink.fails = None
    await setup.clock.advance(seconds=20)

    kept = setup.watch(made)
    assert (kept.state, kept.reason, kept.events) == ("active", None, 1)
    assert len(setup.told) == told + 1
    # It went off late, and keeps its rhythm.
    assert kept.due_at == _START + timedelta(minutes=10)
    assert [event.event_id for event in setup.sink.caught] == [
        f"timer:{(_START + timedelta(minutes=5)).isoformat()}"
    ]
    # Its break could not be told, as the sink was what failed: only that it works again.
    assert setup.sink.changes == [("watch.recovered", "")]


async def test_a_removed_timer_stops_at_once(setup: _Setup) -> None:
    made = await setup.watches.add_timer(setup.one.id, "Check the inbox.", 5)
    await setup.clock.advance()
    assert setup.clock.sleeping == 1

    await setup.watches.remove(setup.one.id, str(made.id), by="user")
    await setup.clock.advance(minutes=30)

    assert setup.sink.events == []
    assert setup.clock.sleeping == 0


async def test_end_for_and_a_deleted_gem_stop_their_timers(setup: _Setup) -> None:
    await setup.watches.add_timer(setup.one.id, "Check the inbox.", 5)
    await setup.watches.add_timer(setup.two.id, "Check the post.", 5)
    await setup.clock.advance()
    assert setup.clock.sleeping == 2

    await setup.watches.end_for(setup.one.id)
    await setup.gems.delete(str(setup.two.id))
    await setup.clock.advance(minutes=30)

    assert setup.sink.events == []
    assert setup.clock.sleeping == 0
    assert setup.stored() == []


# ---- MCP events


async def test_an_mcp_watch_hands_over_each_event_and_keeps_its_cursor(setup: _Setup) -> None:
    made = await setup.watches.add_mcp(
        setup.one.id, "Triage new issues.", _OPENED, {"repo": "example/app"}
    )
    await _until(lambda: str(made.id) in setup.mcp.running)
    [(run, hook_url)] = setup.mcp.runs
    assert run == McpWatch(
        watch_id=str(made.id),
        gem_id=setup.one.id,
        connection_id=_OPENED.connection_id,
        event="issue.opened",
        arguments={"repo": "example/app"},
        cursor=None,
    )
    assert made.token is not None and hook_url == f"{_PUBLIC}/v1/hooks/{made.token}"

    happened = _START - timedelta(minutes=1)
    on_event = setup.mcp.running[str(made.id)]
    await on_event("issue.opened", '{"number": 4}', "e1", happened, "c1")
    await on_event("issue.opened", '{"number": 5}', None, None, None)

    assert setup.sink.events == [
        GemEvent(
            gem_id=setup.one.id,
            watch_id=str(made.id),
            source="mcp",
            name="issue.opened",
            about="Triage new issues.",
            data='{"number": 4}',
            at=happened,
            event_id="e1",
        ),
        GemEvent(
            gem_id=setup.one.id,
            watch_id=str(made.id),
            source="mcp",
            name="issue.opened",
            about="Triage new issues.",
            data='{"number": 5}',
            at=_START,
        ),
    ]
    # An event with no cursor leaves the last one kept.
    assert (setup.watch(made).cursor, setup.watch(made).events) == ("c1", 2)

    # After a restart it goes on from the cursor kept.
    await setup.watches.close()
    assert setup.mcp.stopped == [str(made.id)]
    again = setup.entity()
    await again.start()
    await _until(lambda: len(setup.mcp.runs) == 2)
    assert setup.mcp.runs[1][0].cursor == "c1"
    await again.close()


async def test_an_mcp_watch_with_no_public_address_runs_without_one(
    setup: _Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "public_url", "")
    made = await setup.watches.add_mcp(setup.one.id, "Triage new issues.", _OPENED, {})

    await _until(lambda: str(made.id) in setup.mcp.running)
    assert setup.mcp.runs[0][1] is None


async def test_an_mcp_watch_that_cannot_run_fails_and_comes_back(setup: _Setup) -> None:
    setup.mcp.fails = RuntimeError("the connection is no longer open to the gem")
    made = await setup.watches.add_mcp(setup.one.id, "Triage new issues.", _OPENED, {})
    await _until(lambda: setup.watch(made).state == "failing")
    assert setup.watch(made).reason == "the connection is no longer open to the gem"

    # Tried again after 10 seconds, then 20, then 40.
    await setup.clock.advance(seconds=10)
    await setup.clock.advance(seconds=20)
    assert len(setup.mcp.runs) == 3
    setup.mcp.fails = None
    await setup.clock.advance(seconds=39)
    assert len(setup.mcp.runs) == 3
    await setup.clock.advance(seconds=1)
    assert str(made.id) in setup.mcp.running
    # Running is not yet working: it is active again once it has run a minute.
    assert setup.watch(made).state == "failing"
    await setup.clock.advance(seconds=60)

    kept = setup.watch(made)
    assert (kept.state, kept.reason) == ("active", None)
    told = [watch for _, watch in setup.told]
    assert told == [str(made.id)] * 3  # added, failing, active again


async def test_an_mcp_watch_that_brings_an_event_is_active_again(setup: _Setup) -> None:
    setup.mcp.fails = RuntimeError("the server did not answer")
    made = await setup.watches.add_mcp(setup.one.id, "Triage new issues.", _OPENED, {})
    await _until(lambda: setup.watch(made).state == "failing")
    setup.mcp.fails = None
    await setup.clock.advance(seconds=10)

    await setup.mcp.running[str(made.id)]("issue.opened", "{}", None, None, "c1")

    assert setup.watch(made).state == "active"


async def test_a_watch_that_breaks_and_recovers_tells_its_gem_once_each_time(
    setup: _Setup,
) -> None:
    setup.mcp.fails = RuntimeError("Its server does not answer: timed out")
    made = await setup.watches.add_mcp(setup.one.id, "Triage new issues.", _OPENED, {})
    await _until(lambda: len(setup.sink.changes) == 1)

    [failing] = setup.sink.events
    assert failing == GemEvent(
        gem_id=setup.one.id,
        watch_id=str(made.id),
        source="mcp",
        name="watch.failing",
        about="Triage new issues.",
        data="Its server does not answer: timed out",
        at=_START,
        event_id=f"watch.failing:{_START.isoformat()}",
    )
    # Tried again, and failing again for another reason: the app is told, the gem is not.
    setup.mcp.fails = RuntimeError("The user needs to sign in to Issues again")
    told = len(setup.told)
    await setup.clock.advance(seconds=10)
    await setup.clock.advance(seconds=20)
    assert len(setup.mcp.runs) == 3
    assert setup.watch(made).reason == "The user needs to sign in to Issues again"
    assert len(setup.told) == told + 1
    assert len(setup.sink.events) == 1

    # It works again once it has run a minute.
    setup.mcp.fails = None
    await setup.clock.advance(seconds=40)
    await setup.clock.advance(seconds=60)
    await _until(lambda: len(setup.sink.changes) == 2)
    recovered = setup.sink.events[1]
    assert (recovered.name, recovered.data, recovered.source) == ("watch.recovered", "", "mcp")
    assert recovered.at == _START + timedelta(seconds=130)

    # It breaks again later: told again, under an id of its own.
    setup.mcp.fail(str(made.id), RuntimeError("Its server does not answer: timed out"))
    await _until(lambda: len(setup.sink.changes) == 3)
    again = setup.sink.events[2]
    assert again.name == "watch.failing"
    assert len({event.event_id for event in setup.sink.events}) == 3
    # None of them is counted as an event the watch brought, nor by the fuse.
    kept = setup.watch(made)
    assert (kept.events, kept.last_event_at) == (0, None)
    recent = setup.watches._recent  # pyright: ignore[reportPrivateUsage]
    assert not recent.get(str(made.id))


async def test_a_watch_that_works_again_by_an_event_tells_its_gem_after_the_event(
    setup: _Setup,
) -> None:
    setup.mcp.fails = RuntimeError("the server did not answer")
    made = await setup.watches.add_mcp(setup.one.id, "Triage new issues.", _OPENED, {})
    await _until(lambda: setup.watch(made).state == "failing")
    setup.mcp.fails = None
    await setup.clock.advance(seconds=10)

    await setup.mcp.running[str(made.id)]("issue.opened", "{}", "e1", None, "c1")
    await _until(lambda: len(setup.sink.events) == 3)

    assert [event.name for event in setup.sink.events] == [
        "watch.failing",
        "issue.opened",
        "watch.recovered",
    ]
    assert setup.watch(made).events == 1


async def test_a_sink_that_cannot_be_told_a_break_does_not_stop_the_watch(
    setup: _Setup, caplog: pytest.LogCaptureFixture
) -> None:
    setup.sink.fails = ConnectionError("the database went away")
    setup.mcp.fails = RuntimeError("the server did not answer")
    made = await setup.watches.add_mcp(setup.one.id, "Triage new issues.", _OPENED, {})
    await _until(lambda: "could not be told watch.failing" in caplog.text)

    # It is tried again as ever, and runs once it can.
    setup.mcp.fails = None
    await setup.clock.advance(seconds=10)
    assert len(setup.mcp.runs) == 2
    assert str(made.id) in setup.mcp.running
    assert setup.sink.events == []


async def test_an_mcp_watch_keeps_a_cursor_that_moved_with_no_event(setup: _Setup) -> None:
    made = await setup.watches.add_mcp(
        setup.one.id, "Triage new issues.", _OPENED, {"repo": "example/app"}
    )
    await _until(lambda: str(made.id) in setup.mcp.cursors)

    # A poll that brought nothing, or a stream's start: the place moves, no event comes.
    await setup.mcp.cursors[str(made.id)]("c3")

    assert (setup.watch(made).cursor, setup.watch(made).events) == ("c3", 0)
    assert setup.sink.events == []
    # So after a restart it goes on from there, not from now.
    await setup.watches.close()
    again = setup.entity()
    await again.start()
    await _until(lambda: len(setup.mcp.runs) == 2)
    assert setup.mcp.runs[1][0].cursor == "c3"
    await again.close()


async def test_an_mcp_watch_keeps_the_delivery_it_started_on(setup: _Setup) -> None:
    made = await setup.watches.add_mcp(setup.one.id, "Triage new issues.", _OPENED, {})
    assert made.delivery is None
    await _until(lambda: str(made.id) in setup.mcp.deliveries)
    on_delivery = setup.mcp.deliveries[str(made.id)]
    told = len(setup.told)

    await on_delivery("webhook")
    await on_delivery("webhook")
    await on_delivery("poll")  # as after a server would not take the webhook
    await _until(lambda: len(setup.told) == told + 2)

    assert setup.watch(made).delivery == "poll"
    # Told when it changes only.
    await on_delivery("poll")
    await setup.clock.advance()
    assert len(setup.told) == told + 2


async def test_a_webhook_is_delivered_by_webhook_and_a_timer_by_nothing(setup: _Setup) -> None:
    hook = await setup.watches.add_webhook(setup.one.id, "Review pull requests.")
    timer = await setup.watches.add_timer(setup.one.id, "Check the inbox.", 5)

    assert (setup.watch(hook).delivery, setup.watch(timer).delivery) == ("webhook", None)


async def test_a_watch_stored_without_a_delivery_has_none(setup: _Setup) -> None:
    made = await setup.watches.add_webhook(setup.one.id, "Review pull requests.")
    [stored] = setup.stored()
    del stored["delivery"]

    assert (await setup.watches.get(setup.one.id, str(made.id))).delivery is None


async def test_an_mcp_watch_whose_event_cannot_be_kept_keeps_no_cursor(setup: _Setup) -> None:
    made = await setup.watches.add_mcp(setup.one.id, "Triage new issues.", _OPENED, {})
    await _until(lambda: str(made.id) in setup.mcp.running)
    setup.sink.fails = ConnectionError("the database went away")

    with pytest.raises(ConnectionError):
        await setup.mcp.running[str(made.id)]("issue.opened", "{}", None, None, "c1")

    assert (setup.watch(made).cursor, setup.watch(made).events) == (None, 0)


async def test_a_removed_mcp_watch_stops_at_once(setup: _Setup) -> None:
    made = await setup.watches.add_mcp(setup.one.id, "Triage new issues.", _OPENED, {})
    await _until(lambda: str(made.id) in setup.mcp.running)

    await setup.watches.remove(setup.one.id, str(made.id), by="gem")

    assert setup.mcp.stopped == [str(made.id)]
    assert setup.mcp.running == {}


# ---- the hooks route


def _signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
async def hooks(setup: _Setup) -> AsyncGenerator[httpx.AsyncClient]:
    """A client of the app, in the test's own loop, whose hooks route reaches `setup`'s watches."""
    app.dependency_overrides[get_hooks_service_v1] = lambda: HooksServiceV1Class(setup.watches)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def _webhook(setup: _Setup) -> tuple[Watch, str]:
    made = await setup.watches.add_webhook(setup.one.id, "Review each new pull request.")
    assert made.token is not None
    return made, made.token


async def test_a_post_to_an_unknown_address_is_404_and_says_nothing(
    setup: _Setup, hooks: httpx.AsyncClient
) -> None:
    await _webhook(setup)

    for token in ["nope", "x" * 43]:
        answer = await hooks.post(f"/v1/hooks/{token}", content=b"hello")
        assert (answer.status_code, answer.content) == (404, b"")
    assert setup.sink.events == []


async def test_an_unsigned_post_wakes_the_gem(setup: _Setup, hooks: httpx.AsyncClient) -> None:
    made, token = await _webhook(setup)

    answer = await hooks.post(f"/v1/hooks/{token}", content=b'{"action": "opened"}')

    assert (answer.status_code, answer.content) == (202, b"")
    assert setup.sink.events == [
        GemEvent(
            gem_id=setup.one.id,
            watch_id=str(made.id),
            source="webhook",
            name="webhook",
            about="Review each new pull request.",
            data='{"action": "opened"}',
            at=_START,
        )
    ]
    assert setup.watch(made).events == 1


async def test_a_github_post_signed_with_the_token_wakes_the_gem(
    setup: _Setup, hooks: httpx.AsyncClient
) -> None:
    _, token = await _webhook(setup)
    body = b'{"action": "opened", "number": 7}'

    answer = await hooks.post(
        f"/v1/hooks/{token}",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "72d3162e-cc78-11e3-81ab-4c9367dc0958",
            "X-Hub-Signature-256": _signature(token, body),
            "Content-Type": "application/json",
        },
    )

    assert answer.status_code == 202
    [event] = setup.sink.events
    assert (event.name, event.event_id, event.data) == (
        "pull_request",
        "72d3162e-cc78-11e3-81ab-4c9367dc0958",
        body.decode(),
    )


async def test_a_post_whose_signature_is_not_the_token_s_is_401(
    setup: _Setup, hooks: httpx.AsyncClient
) -> None:
    _, token = await _webhook(setup)
    body = b'{"secret_looking": "do-not-echo"}'

    for signature in [
        _signature("another", body).encode(),
        b"sha256=",
        b"nonsense",
        "sha256=é".encode("latin-1"),
    ]:
        answer = await hooks.post(
            f"/v1/hooks/{token}", content=body, headers={b"X-Hub-Signature-256": signature}
        )
        assert (answer.status_code, answer.content) == (401, b"")
    assert setup.sink.events == []


async def test_a_github_ping_is_answered_and_wakes_nobody(
    setup: _Setup, hooks: httpx.AsyncClient
) -> None:
    made, token = await _webhook(setup)
    body = b'{"zen": "Keep it logically awesome."}'

    answer = await hooks.post(
        f"/v1/hooks/{token}",
        content=body,
        headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": _signature(token, body)},
    )

    assert (answer.status_code, answer.content) == (200, b"")
    assert setup.sink.events == []
    assert setup.watch(made).events == 0


async def test_a_post_over_a_megabyte_is_413_and_not_handed_over(
    setup: _Setup, hooks: httpx.AsyncClient
) -> None:
    _, token = await _webhook(setup)
    big = b"x" * (1024 * 1024 + 1)

    answer = await hooks.post(f"/v1/hooks/{token}", content=big)

    assert (answer.status_code, answer.content) == (413, b"")

    async def streamed() -> AsyncGenerator[bytes]:
        # With no length said beforehand: it is counted as it is read.
        for _ in range(17):
            yield b"y" * 65536

    answer = await hooks.post(f"/v1/hooks/{token}", content=streamed())
    assert (answer.status_code, answer.content) == (413, b"")
    assert setup.sink.events == []
    # A megabyte exactly is taken, and cut for the model.
    answer = await hooks.post(f"/v1/hooks/{token}", content=b"z" * (1024 * 1024))
    assert answer.status_code == 202
    [event] = setup.sink.events
    assert len(event.data) == EVENT_TEXT_LIMIT
    assert event.data.endswith(
        "[The event was 1048576 characters long. Only the first part is shown.]"
    )


async def test_a_body_that_is_not_utf8_is_handed_over_as_text(
    setup: _Setup, hooks: httpx.AsyncClient
) -> None:
    _, token = await _webhook(setup)

    answer = await hooks.post(f"/v1/hooks/{token}", content=b"caf\xe9")

    assert answer.status_code == 202
    assert setup.sink.events[0].data == "caf\ufffd"


async def test_a_post_that_cannot_be_kept_is_503_and_counts_nothing(
    setup: _Setup, hooks: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    made, token = await _webhook(setup)
    setup.sink.fails = ConnectionError("the database went away")

    answer = await hooks.post(f"/v1/hooks/{token}", content=b"private words")

    assert (answer.status_code, answer.content) == (503, b"")
    assert setup.watch(made).events == 0
    assert "private words" not in caplog.text
    assert "ConnectionError" in caplog.text


async def test_a_post_to_an_mcp_watch_is_read_by_the_connections(
    setup: _Setup, hooks: httpx.AsyncClient
) -> None:
    made = await setup.watches.add_mcp(setup.one.id, "Triage new issues.", _OPENED, {})
    assert made.token is not None

    answer = await hooks.post(
        f"/v1/hooks/{made.token}", content=b'{"n": 1}', headers={"X-Server-Signature": "s"}
    )

    assert answer.status_code == 202
    [(watch, headers, body)] = setup.mcp.posts
    assert (watch.watch_id, headers["x-server-signature"], body) == (str(made.id), "s", b'{"n": 1}')
    assert [(event.source, event.event_id) for event in setup.sink.events] == [("mcp", "p1")]
    assert setup.watch(made).cursor == "c9"

    setup.mcp.signed = False
    answer = await hooks.post(f"/v1/hooks/{made.token}", content=b'{"n": 2}')
    assert (answer.status_code, answer.content) == (401, b"")
    assert len(setup.sink.events) == 1


async def test_a_timer_has_no_address(setup: _Setup) -> None:
    made = await setup.watches.add_timer(setup.one.id, "Check the inbox.", 5)

    assert (made.token, made.hook_url) == (None, None)
    assert await setup.watches.find_hook("") is None


# ---- the fuse


async def test_a_watch_that_brings_61_events_in_an_hour_is_paused(
    setup: _Setup, hooks: httpx.AsyncClient
) -> None:
    made, token = await _webhook(setup)
    for _ in range(60):
        assert (await hooks.post(f"/v1/hooks/{token}", content=b"{}")).status_code == 202
        await setup.clock.advance(seconds=59)
    await _until(lambda: len(setup.told) == 1)

    answer = await hooks.post(f"/v1/hooks/{token}", content=b"{}")

    assert (answer.status_code, answer.content) == (409, b"")
    assert len(setup.sink.caught) == 60
    kept = setup.watch(made)
    assert kept.state == "paused"
    assert kept.reason is not None and "more than 60 events in an hour" in kept.reason
    await _until(lambda: len(setup.told) == 2)
    # The gem is told once, with why, though the watch is paused and the fuse has blown.
    assert setup.sink.changes == [("watch.paused", kept.reason)]
    assert kept.events == 60
    # It brings no more, an hour later too.
    await setup.clock.advance(hours=2)
    assert (await hooks.post(f"/v1/hooks/{token}", content=b"{}")).status_code == 409
    assert len(setup.sink.caught) == 60
    assert len(setup.sink.changes) == 1


async def test_the_fuse_counts_only_the_last_hour(setup: _Setup, hooks: httpx.AsyncClient) -> None:
    made, token = await _webhook(setup)
    for _ in range(60):
        assert (await hooks.post(f"/v1/hooks/{token}", content=b"{}")).status_code == 202
    await setup.clock.advance(hours=1)

    assert (await hooks.post(f"/v1/hooks/{token}", content=b"{}")).status_code == 202
    assert setup.watch(made).state == "active"


async def test_a_paused_mcp_watch_stops_running_and_does_not_run_again(setup: _Setup) -> None:
    made = await setup.watches.add_mcp(setup.one.id, "Triage new issues.", _OPENED, {})
    await _until(lambda: str(made.id) in setup.mcp.running)
    for number in range(61):
        on_event = setup.mcp.running.get(str(made.id))
        assert on_event is not None, number
        await on_event("issue.opened", "{}", None, None, f"c{number}")
    await _until(lambda: setup.mcp.stopped == [str(made.id)])

    assert setup.watch(made).state == "paused"
    assert (len(setup.sink.caught), setup.watch(made).cursor) == (60, "c59")
    assert [name for name, _ in setup.sink.changes] == ["watch.paused"]
    await setup.watches.close()
    again = setup.entity()
    await again.start()
    await setup.clock.advance(minutes=30)
    assert len(setup.mcp.runs) == 1
    await again.close()


# ---- the gem's tools


async def _call(setup: _Setup, name: str, **arguments: object) -> tuple[str, bool]:
    """Call one of Gem One's watch tools as the loop does: its text, and whether it failed."""
    box = Toolbox(setup.watches.tools_for(setup.one.id))
    shown: list[str] = []
    result = await box.attempt(name, json.dumps(arguments), show=shown.append)
    assert len(shown) == 1
    return result


async def test_the_tools_are_the_same_four_for_every_gem(setup: _Setup) -> None:
    one = [tool.spec for tool in setup.watches.tools_for(setup.one.id)]
    two = [tool.spec for tool in setup.watches.tools_for(setup.two.id)]

    assert one == two
    assert [spec.name for spec in one] == ["watch", "unwatch", "watches", "watchable"]
    for spec in one:
        # Flat objects, with nothing at the top a provider refuses there.
        assert spec.parameters["type"] == "object"
        assert not {"anyOf", "oneOf", "allOf", "enum", "not"} & set(spec.parameters)
        json.dumps(spec.parameters)


async def test_a_gem_sets_a_timer(setup: _Setup) -> None:
    text, failed = await _call(
        setup, "watch", about="Check the inbox.", source="timer", every_minutes=30
    )

    [made] = await setup.watches.list_for(setup.one.id)
    assert not failed
    assert text == (
        f"Watching, under the id {made.id}: a timer every 30 minutes. It first goes off at "
        "2026-11-26 09:30 UTC."
    )
    assert (made.source, made.every_minutes, made.about) == ("timer", 30, "Check the inbox.")


async def test_a_timer_too_often_or_without_minutes_is_refused_in_words(setup: _Setup) -> None:
    assert await _call(setup, "watch", about="Nag.", source="timer", every_minutes=1) == (
        "watch failed: A timer goes off at most every 5 minutes",
        True,
    )
    for every in [None, "10", 2.5, True]:
        text, failed = await _call(
            setup, "watch", about="Nag.", source="timer", every_minutes=every
        )
        assert failed and "`every_minutes`" in text
    assert setup.stored() == []


async def test_a_gem_is_given_a_webhook_to_register(setup: _Setup) -> None:
    text, failed = await _call(
        setup, "watch", about="Review each new pull request.", source="webhook"
    )

    [made] = await setup.watches.list_for(setup.one.id)
    assert not failed and made.token is not None
    assert text == (
        f"Watching, under the id {made.id}. Register this webhook where its events come from:\n"
        f"Address: {_PUBLIC}/v1/hooks/{made.token}\n"
        f"Secret: {made.token}\n"
        "Content type: application/json\n"
        "Each post to it wakes you. The secret is for that registration only: do not show it "
        "to anyone."
    )


async def test_a_webhook_with_no_public_address_is_refused_in_words(
    setup: _Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "public_url", "")

    text, failed = await _call(setup, "watch", about="Review pull requests.", source="webhook")

    assert failed
    assert text == (
        "watch failed: This backend has no public address (PUBLIC_URL), so nothing outside "
        "can post to it"
    )


async def test_a_gem_watches_an_event_of_a_connection_open_to_it(setup: _Setup) -> None:
    setup.mcp.open[setup.one.id] = [_OPENED]

    text, failed = await _call(
        setup,
        "watch",
        about="Triage new issues.",
        source="event",
        connection="issue_tracker",
        event="issue.opened",
        arguments={"repo": "example/app"},
    )

    [made] = await setup.watches.list_for(setup.one.id)
    assert (text, failed) == (
        f"Watching issue_tracker's issue.opened events, under the id {made.id}.",
        False,
    )
    assert made.mcp.arguments == {"repo": "example/app"}
    await _until(lambda: str(made.id) in setup.mcp.running)


async def test_an_event_not_open_to_the_gem_is_refused_in_words(setup: _Setup) -> None:
    setup.mcp.open[setup.two.id] = [_OPENED]

    for connection, event in [("issue_tracker", "issue.opened"), ("other", "issue.opened")]:
        text, failed = await _call(
            setup, "watch", about="Triage.", source="event", connection=connection, event=event
        )
        assert failed
        assert text == (
            f"watch failed: No connection open to you sends the event 'issue.opened' of "
            f"{connection!r}: `watchable` lists the ones you can watch"
        )
    assert setup.stored() == []


async def test_a_watch_with_no_about_or_source_is_refused_in_words(setup: _Setup) -> None:
    assert await _call(setup, "watch", about=" ", source="timer", every_minutes=5) == (
        "watch failed: Say what the watch is for, in `about`",
        True,
    )
    assert await _call(setup, "watch", about="Nag.", source="email") == (
        'watch failed: `source` is "event", "webhook" or "timer"',
        True,
    )


async def test_a_gem_lists_its_watches(setup: _Setup) -> None:
    assert await _call(setup, "watches") == ("You watch nothing now.", False)
    timer = await setup.watches.add_timer(setup.one.id, "Check the inbox.", 5)
    hook = await setup.watches.add_webhook(setup.one.id, "Review pull requests.")
    await setup.watches.add_webhook(setup.two.id, "Not mine.")
    setup.mcp.fails = RuntimeError("the server did not answer")
    event = await setup.watches.add_mcp(setup.one.id, "Triage new issues.", _OPENED, {})
    await _until(lambda: setup.watch(event).state == "failing")
    await setup.clock.advance(minutes=5)

    text, failed = await _call(setup, "watches")

    assert not failed
    assert text == "\n".join(
        [
            f"- {event.id}: issue_tracker's issue.opened events. For: Triage new issues. "
            "State: failing: the server did not answer. 0 events so far.",
            f"- {hook.id}: a webhook. For: Review pull requests. State: active. 0 events so far.",
            f"- {timer.id}: a timer every 5 minutes, next at 2026-11-26 09:10 UTC. For: Check "
            "the inbox. State: active. 1 event so far, the last at 2026-11-26 09:05 UTC.",
        ]
    )
    assert hook.token is not None and hook.token not in text


async def test_a_gem_removes_a_watch_of_its_own_only(setup: _Setup) -> None:
    mine = await setup.watches.add_timer(setup.one.id, "Check the inbox.", 5)
    theirs = await setup.watches.add_timer(setup.two.id, "Check the post.", 5)

    assert await _call(setup, "unwatch", id=str(theirs.id)) == (
        f"unwatch failed: You have no watch with the id '{theirs.id}': `watches` lists yours",
        True,
    )
    assert await _call(setup, "unwatch", id=str(mine.id)) == (
        f"Removed the watch {mine.id}: it wakes you no more.",
        False,
    )
    assert [d["_id"] for d in setup.stored()] == [theirs.id]
    assert ("watch.removed", {"watch": str(mine.id), "by": "gem"}) in await setup.logged(setup.one)


async def test_a_gem_lists_what_it_can_watch(setup: _Setup) -> None:
    assert await _call(setup, "watchable") == (
        "No connection open to you sends events you can watch. A webhook or a timer can still "
        "be watched.",
        False,
    )
    setup.mcp.open[setup.one.id] = [_OPENED]

    assert await _call(setup, "watchable") == (
        "- connection issue_tracker, event issue.opened: An issue was opened. Arguments: "
        '{"type": "object", "properties": {"repo": {"type": "string"}}}',
        False,
    )


# ---- the app's routes


@pytest.fixture
async def routes(setup: _Setup) -> AsyncGenerator[httpx.AsyncClient]:
    """A client of the app, in the test's own loop, whose watches routes reach `setup`'s."""
    service = WatchesServiceV1Class(setup.watches, setup.gems)
    app.dependency_overrides[get_watches_service_v1] = lambda: service
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def test_the_app_lists_a_gem_s_watches_without_their_secrets(
    setup: _Setup, routes: httpx.AsyncClient
) -> None:
    timer = await setup.watches.add_timer(setup.one.id, "Check the inbox.", 5)
    await setup.clock.advance(seconds=1)
    hook = await setup.watches.add_webhook(setup.one.id, "Review pull requests.")
    await setup.clock.advance(seconds=1)
    event = await setup.watches.add_mcp(
        setup.one.id, "Triage new issues.", _OPENED, {"repo": "example/app"}
    )
    await setup.watches.add_webhook(setup.two.id, "Not hers.")
    await _until(lambda: str(event.id) in setup.mcp.deliveries)
    await setup.mcp.deliveries[str(event.id)]("push")

    answer = await routes.get(f"/v1/gems/{setup.one.id}/watches")

    assert answer.status_code == 200
    for made in (hook, event):
        assert made.token is not None and made.token not in answer.text
    assert "/v1/hooks" not in answer.text
    assert answer.json() == [
        {
            "id": str(event.id),
            "gem": str(setup.one.id),
            "about": "Triage new issues.",
            "source": "mcp",
            "delivery": "push",
            "state": "active",
            "reason": None,
            "connection": _OPENED.connection_id,
            "event": "issue.opened",
            "arguments": {"repo": "example/app"},
            "every_minutes": None,
            "next_at": None,
            "has_address": False,
            "created_at": "2026-11-26T09:00:02Z",
            "last_event_at": None,
            "events": 0,
        },
        {
            "id": str(hook.id),
            "gem": str(setup.one.id),
            "about": "Review pull requests.",
            "source": "webhook",
            "delivery": "webhook",
            "state": "active",
            "reason": None,
            "connection": None,
            "event": None,
            "arguments": None,
            "every_minutes": None,
            "next_at": None,
            "has_address": True,
            "created_at": "2026-11-26T09:00:01Z",
            "last_event_at": None,
            "events": 0,
        },
        {
            "id": str(timer.id),
            "gem": str(setup.one.id),
            "about": "Check the inbox.",
            "source": "timer",
            "delivery": None,
            "state": "active",
            "reason": None,
            "connection": None,
            "event": None,
            "arguments": None,
            "every_minutes": 5,
            "next_at": "2026-11-26T09:05:00Z",
            "has_address": False,
            "created_at": "2026-11-26T09:00:00Z",
            "last_event_at": None,
            "events": 0,
        },
    ]


async def test_the_watches_of_a_gem_that_is_not_there_are_404(
    setup: _Setup, routes: httpx.AsyncClient
) -> None:
    for gem in [str(ObjectId()), "nope"]:
        assert (await routes.get(f"/v1/gems/{gem}/watches")).status_code == 404


async def test_the_user_removes_a_watch_and_it_stops(
    setup: _Setup, routes: httpx.AsyncClient
) -> None:
    made = await setup.watches.add_timer(setup.one.id, "Check the inbox.", 5)
    theirs = await setup.watches.add_timer(setup.two.id, "Check the post.", 5)

    answer = await routes.delete(f"/v1/gems/{setup.one.id}/watches/{made.id}")

    assert (answer.status_code, answer.content) == (204, b"")
    assert [d["_id"] for d in setup.stored()] == [theirs.id]
    assert ("watch.removed", {"watch": str(made.id), "by": "user"}) in await setup.logged(setup.one)
    await setup.clock.advance(minutes=5)
    assert [event.watch_id for event in setup.sink.events] == [str(theirs.id)]
    # Another gem's watch, one that is gone, and one that never was are not found.
    for gem, watch in [(setup.one.id, theirs.id), (setup.one.id, made.id), (setup.one.id, "x")]:
        answer = await routes.delete(f"/v1/gems/{gem}/watches/{watch}")
        assert answer.status_code == 404


# ---- the standing socket


async def test_every_socket_is_told_when_a_watch_changes(setup: _Setup) -> None:
    chat = await _chat()
    plain = chat.service()
    link = LinkServiceV1Class(chat.chat, plain.call, watches=setup.watches)
    first, first_linked = await _open(link)
    second, second_linked = await _open(link)

    made = await setup.watches.add_timer(setup.one.id, "Check the inbox.", 5)
    setup.sink.fails = ConnectionError("the database went away")
    await setup.clock.advance(minutes=5)
    await setup.watches.remove(setup.one.id, str(made.id), by="user")

    told = {"type": "watch.changed", "gem": str(setup.one.id), "watch": str(made.id)}
    for socket in (first, second):
        await socket.until("watch.changed", 3)  # added, failing, removed
        assert [f for f in socket.frames if f["type"] != "link.ready"] == [told] * 3
    await _close(first, first_linked)
    await _close(second, second_linked)
    # A socket that has gone is told no more.
    listeners = setup.watches._listeners  # pyright: ignore[reportPrivateUsage]
    assert len(listeners) == 1
