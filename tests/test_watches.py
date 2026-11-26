"""Watches: what a gem watches for, kept, run and removed.

The database, the event sink, the connections' MCP events and the clock are stand-ins,
so a timer's hours pass at once and nothing reaches the internet.
"""

import asyncio
import hashlib
import hmac
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
    OnMcpEvent,
)
from app.entities.gem_log import LogEntityClass
from app.entities.watch_ent import Watch, WatchEntityClass
from app.main import app
from app.routers.hooks.service import HooksServiceV1Class, get_hooks_service_v1
from tests.test_chat import _DocDB, _docdb  # pyright: ignore[reportPrivateUsage]

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


class _Sink:
    """The event sink: it keeps what it is handed, or fails while `fails` is set."""

    def __init__(self) -> None:
        self.events: list[GemEvent] = []
        self.fails: Exception | None = None

    async def arrived(self, event: GemEvent) -> None:
        if self.fails is not None:
            raise self.fails
        self.events.append(event)


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
        self.stopped: list[str] = []
        self.fails: Exception | None = None
        # Whether a post is one the server signed, and what each was posted with.
        self.signed = True
        self.posts: list[tuple[McpWatch, dict[str, str], bytes]] = []

    async def kinds_for(self, gem_id: ObjectId) -> list[McpEventKind]:
        return self.open.get(gem_id, [])

    async def watch(self, watch: McpWatch, on_event: OnMcpEvent, hook_url: str | None) -> None:
        self.runs.append((watch, hook_url))
        if self.fails is not None:
            raise self.fails
        self.running[watch.watch_id] = on_event
        try:
            await asyncio.Event().wait()
        finally:
            del self.running[watch.watch_id]
            self.stopped.append(watch.watch_id)

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
    assert [event.event_id for event in setup.sink.events] == [
        f"timer:{(_START + timedelta(minutes=5)).isoformat()}"
    ]


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
    assert len(setup.sink.events) == 60
    kept = setup.watch(made)
    assert kept.state == "paused"
    assert kept.reason is not None and "more than 60 events in an hour" in kept.reason
    await _until(lambda: len(setup.told) == 2)
    # It brings no more, an hour later too.
    await setup.clock.advance(hours=2)
    assert (await hooks.post(f"/v1/hooks/{token}", content=b"{}")).status_code == 409
    assert len(setup.sink.events) == 60


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
    assert (len(setup.sink.events), setup.watch(made).cursor) == (60, "c59")
    await setup.watches.close()
    again = setup.entity()
    await again.start()
    await setup.clock.advance(minutes=30)
    assert len(setup.mcp.runs) == 1
    await again.close()
