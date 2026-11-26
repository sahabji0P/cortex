"""A connection's MCP events: its copy of the kinds its server serves, and watching one.

The server is the one in `tests/mcp_events_server.py`, on a port of this machine,
reached through the real adapter. Only the database is faked.
"""

import asyncio
import base64
import dataclasses
import json
from collections.abc import AsyncGenerator, Mapping
from datetime import datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from bson import ObjectId

from app.adapters.mcp_adp import McpAdapterClass, McpFailure
from app.entities.connection_ent import Connection, ConnectionEntityClass, ConnectionEvent
from app.entities.gem_ent import GemConfig, GemEntityClass
from app.entities.gem_event import (
    EVENT_TEXT_LIMIT,
    McpEventKind,
    McpEvents,
    McpWatch,
    WatchDelivery,
)
from app.entities.mcp_events_ent import McpEventsEntityClass, WatchFailed
from app.routers.connections.models import ConnectionsModels
from app.routers.connections.service import ConnectionsServiceV1Class
from tests.mcp_events_server import KINDS, EventsServer, Running, running
from tests.test_chat import _DocDB, _docdb  # pyright: ignore[reportPrivateUsage]

if TYPE_CHECKING:
    from app.adapters.docdb_adp import DocDBAdapterClass


class _Setup(SimpleNamespace):
    docdb: _DocDB
    served: Running
    mcp: McpAdapterClass
    connections: ConnectionEntityClass
    one: GemConfig
    two: GemConfig


async def _setup(server: EventsServer) -> AsyncGenerator[_Setup]:
    docdb, adapter = _docdb()
    mcp = McpAdapterClass()
    gems = GemEntityClass(adapter)
    async with running(server) as served:
        yield _Setup(
            docdb=docdb,
            served=served,
            mcp=mcp,
            connections=ConnectionEntityClass(docdb=adapter, mcp=mcp, gems=gems),
            one=await gems.create(name="Gem One", model="6 Luna"),
            two=await gems.create(name="Gem Two", model="6 Luna"),
        )
        await mcp.close()


@pytest.fixture
async def setup() -> AsyncGenerator[_Setup]:
    async for made in _setup(EventsServer()):
        yield made


async def test_a_connection_keeps_the_kinds_of_event_its_server_serves(setup: _Setup) -> None:
    service = ConnectionsServiceV1Class(setup.connections)

    made = await service.create_connection(
        ConnectionsModels.V1.Input.CreateConnection(name="Events", url=setup.served.url)
    )

    assert [(event.name, event.description, event.delivery) for event in made.events] == [
        ("issue.opened", "Fires when an issue is opened in a repo", ["poll", "push", "webhook"]),
        ("build.finished", "Fires when a build ends", ["poll"]),
    ]
    kept = (await setup.connections.get(made.id)).events
    assert json.loads(kept[0].input_schema) == KINDS[0]["inputSchema"]
    assert [tool.name for tool in made.tools] == []


async def test_the_copy_of_the_events_is_read_again_only_when_the_user_asks(
    setup: _Setup,
) -> None:
    made = await setup.connections.create(name="Events", url=setup.served.url)
    setup.served.server.kinds = [KINDS[1]]

    unchanged = await setup.connections.get(str(made.id))
    refreshed = await setup.connections.refresh(str(made.id))

    assert [event.name for event in unchanged.events] == ["issue.opened", "build.finished"]
    assert [event.name for event in refreshed.events] == ["build.finished"]
    assert setup.served.server.asked == ["events/list", "events/list"]


async def test_kinds_of_event_that_cannot_be_watched_are_left_out() -> None:
    def kind(name: str, **changed: Any) -> dict[str, Any]:
        return {**KINDS[1], "name": name, **changed}

    kinds = [
        kind("plain"),
        kind("plain", description="listed twice"),
        kind(""),
        kind("no.way", delivery=["carrier-pigeon"]),
        kind("not.an.object", inputSchema={"type": "string"}),
        kind("bad.schema", inputSchema={"type": "object", "properties": {"a": {"type": 5}}}),
        kind(
            "in.a.header",
            inputSchema={
                "type": "object",
                "properties": {"region": {"type": "string", "x-mcp-header": "Region"}},
            },
        ),
        kind("some.ways", delivery=["push", "carrier-pigeon"]),
    ]
    async for setup in _setup(EventsServer(kinds=kinds)):
        made = await setup.connections.create(name="Events", url=setup.served.url)

        assert made.events == [
            ConnectionEvent(
                name="plain",
                description="Fires when a build ends",
                delivery=["poll"],
                input_schema='{"type": "object", "properties": {}}',
            ),
            ConnectionEvent(
                name="some.ways",
                description="Fires when a build ends",
                delivery=["push"],
                input_schema='{"type": "object", "properties": {}}',
            ),
        ]


class _Told:
    """What a watch told: each event, as `on_event` was given it."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, str | None, datetime | None, str | None]] = []
        self.came = asyncio.Event()
        # When set, the next event cannot be kept, once.
        self.fail_once = False

    async def __call__(
        self, name: str, text: str, event_id: str | None, at: datetime | None, cursor: str | None
    ) -> None:
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("the store is down")
        self.events.append((name, text, event_id, at, cursor))
        self.came.set()

    async def wait_for(self, count: int) -> None:
        async with asyncio.timeout(5):
            while len(self.events) < count:
                self.came.clear()
                await self.came.wait()

    @property
    def texts(self) -> list[str]:
        return [text for _, text, _, _, _ in self.events]


class _Deliveries:
    """The deliveries a watch said it started on, in order. It may be made to fail."""

    def __init__(self, fails: bool = False) -> None:
        self.seen: list[WatchDelivery] = []
        self.fails = fails

    async def __call__(self, delivery: WatchDelivery) -> None:
        self.seen.append(delivery)
        if self.fails:
            raise RuntimeError("the store is down")


def _events(setup: _Setup) -> McpEvents:
    return McpEventsEntityClass(
        docdb=cast("DocDBAdapterClass", setup.docdb),
        mcp=setup.mcp,
        connections=setup.connections,
        poll_floor=0.01,
        poll_every=0.05,
        pause=(0.01, 0.05),
    )


async def _open_to(setup: _Setup, *gems: GemConfig) -> Connection:
    return await setup.connections.create(
        name="Events", url=setup.served.url, gems=[str(gem.id) for gem in gems]
    )


def _watch(gem: GemConfig, connection: Connection, event: str, **arguments: Any) -> McpWatch:
    return McpWatch(
        watch_id=f"w_{event}",
        gem_id=gem.id,
        connection_id=str(connection.id),
        event=event,
        arguments=arguments,
    )


async def test_a_gem_is_offered_the_events_of_the_connections_open_to_it(setup: _Setup) -> None:
    events = _events(setup)
    connection = await _open_to(setup, setup.one)

    one = await events.kinds_for(setup.one.id)

    assert [(kind.name, kind.delivery) for kind in one] == [
        ("issue.opened", ["poll", "push", "webhook"]),
        ("build.finished", ["poll"]),
    ]
    assert one[0] == McpEventKind(
        connection_id=str(connection.id),
        connection_key="events",
        name="issue.opened",
        description="Fires when an issue is opened in a repo",
        delivery=["poll", "push", "webhook"],
        input_schema=KINDS[0]["inputSchema"],
    )
    assert await events.kinds_for(setup.two.id) == []
    await setup.connections.update(str(connection.id), {"gems": "all"})
    assert len(await events.kinds_for(setup.two.id)) == 2
    await setup.connections.update(str(connection.id), {"enabled": False})
    assert await events.kinds_for(setup.one.id) == []


async def test_a_watch_by_poll_tells_each_event_once(setup: _Setup) -> None:
    setup.served.server.next_poll_ms = 10
    events, told = _events(setup), _Told()
    connection = await _open_to(setup, setup.one)
    watching = asyncio.create_task(
        events.watch(_watch(setup.one, connection, "build.finished"), told, None)
    )
    await _asked(setup.served.server, "events/poll")

    await setup.served.server.emit("build.finished", {"ok": True}, event_id="b1")
    await setup.served.server.emit("build.finished", {"ok": True}, event_id="b1")
    await setup.served.server.emit("build.finished", {"ok": False, "log": "x" * 30_000})
    await told.wait_for(2)
    await asyncio.sleep(0.1)
    await _stop(watching)

    name, text, event_id, at, cursor = told.events[0]
    # The batch's cursor goes with its last event; the ones before carry the one it was
    # asked from.
    assert (name, text, event_id, cursor) == ("build.finished", '{"ok":true}', "b1", "0")
    assert at is not None
    assert len(told.events) == 2  # the second b1 is the same event
    assert told.texts[1].startswith('{"ok":false,"log":"xxx')
    assert told.texts[1].endswith(f"Only the first {EVENT_TEXT_LIMIT} are shown.]")
    assert told.events[1][4] == "3"
    assert set(setup.served.server.asked) == {"events/list", "events/poll"}


async def test_a_watch_by_stream_comes_back_from_its_cursor_after_a_drop(setup: _Setup) -> None:
    events, told = _events(setup), _Told()
    connection = await _open_to(setup, setup.one)
    watch = _watch(setup.one, connection, "issue.opened", repo="owner/one")
    watching = asyncio.create_task(events.watch(watch, told, None))
    await _asked(setup.served.server, "events/stream")
    await setup.served.server.emit("issue.opened", {"repo": "owner/one", "number": 1})
    await told.wait_for(1)

    setup.served.server.drop()
    await setup.served.server.emit("issue.opened", {"repo": "owner/one", "number": 2})
    await setup.served.server.emit("issue.opened", {"repo": "owner/two", "number": 3})
    await told.wait_for(2)
    # An event that cannot be kept is asked for again, and told once it is.
    told.fail_once = True
    await setup.served.server.emit("issue.opened", {"repo": "owner/one", "number": 4})
    await told.wait_for(3)
    await _stop(watching)

    assert told.texts == [
        '{"repo":"owner/one","number":1}',
        '{"repo":"owner/one","number":2}',
        '{"repo":"owner/one","number":4}',
    ]
    assert [cursor for *_, cursor in told.events] == ["1", "2", "4"]
    assert setup.served.server.asked.count("events/stream") >= 3


async def test_a_watch_says_which_delivery_it_started_on(setup: _Setup) -> None:
    setup.served.server.next_poll_ms = 10
    events, told = _events(setup), _Told()
    connection = await _open_to(setup, setup.one)
    streamed, polled = _Deliveries(), _Deliveries(fails=True)
    watches = [
        # One the server streams, and one it only lets be polled.
        events.watch(
            _watch(setup.one, connection, "issue.opened", repo="owner/one"),
            told,
            None,
            on_delivery=streamed,
        ),
        events.watch(
            _watch(setup.one, connection, "build.finished"), told, None, on_delivery=polled
        ),
    ]
    watching = [asyncio.create_task(watch) for watch in watches]
    await _asked(setup.served.server, "events/stream")
    await _asked(setup.served.server, "events/poll")

    # One that cannot be kept stops nothing: the watch goes on, and still tells events.
    await setup.served.server.emit("build.finished", {"ok": True})
    await told.wait_for(1)
    for task in watching:
        await _stop(task)

    assert (streamed.seen, polled.seen) == (["push"], ["poll"])


async def test_a_watch_stops_and_says_why_when_it_cannot_run(setup: _Setup) -> None:
    events, told = _events(setup), _Told()
    connection = await _open_to(setup, setup.one)

    with pytest.raises(WatchFailed, match="no longer open to this gem"):
        await events.watch(_watch(setup.two, connection, "build.finished"), told, None)
    with pytest.raises(WatchFailed, match=r"does not serve nothing\.happened"):
        await events.watch(_watch(setup.one, connection, "nothing.happened"), told, None)
    with pytest.raises(WatchFailed, match="refused the watch: a repo is needed"):
        await events.watch(_watch(setup.one, connection, "issue.opened"), told, None)
    missing = connection.model_copy(update={"id": ObjectId()})
    with pytest.raises(WatchFailed, match="removed"):
        await events.watch(_watch(setup.one, missing, "build.finished"), told, None)


async def test_a_watch_stops_when_its_connection_is_closed_to_its_gem(setup: _Setup) -> None:
    setup.served.server.next_poll_ms = 10
    events, told = _events(setup), _Told()
    connection = await _open_to(setup, setup.one)
    watching = asyncio.create_task(
        events.watch(_watch(setup.one, connection, "build.finished"), told, None)
    )
    await _asked(setup.served.server, "events/poll")

    await setup.connections.update(str(connection.id), {"gems": [str(setup.two.id)]})

    with pytest.raises(WatchFailed, match="no longer open to this gem"):
        async with asyncio.timeout(5):
            await watching


async def test_a_watch_stops_when_its_connection_moves_to_another_server(setup: _Setup) -> None:
    setup.served.server.next_poll_ms = 10
    events, told = _events(setup), _Told()
    connection = await _open_to(setup, setup.one)
    watching = asyncio.create_task(
        events.watch(_watch(setup.one, connection, "build.finished"), told, None)
    )
    await _asked(setup.served.server, "events/poll")

    # The same server here, but by another address: to the watch, another server.
    moved = setup.served.url.replace("127.0.0.1", "localhost")
    await setup.connections.update(str(connection.id), {"url": moved})

    with pytest.raises(WatchFailed, match="now points at another server"):
        async with asyncio.timeout(5):
            await watching


async def test_a_watch_whose_server_refuses_its_pass_says_to_sign_in(setup: _Setup) -> None:
    events, told = _events(setup), _Told()
    connection = await _open_to(setup, setup.one)
    setup.served.server.pass_needed = "a-new-one"

    with pytest.raises(WatchFailed, match="needs to sign in to Events again"):
        await events.watch(
            _watch(setup.one, connection, "issue.opened", repo="owner/one"), told, None
        )
    with pytest.raises(WatchFailed, match="needs to sign in to Events again"):
        await events.watch(_watch(setup.one, connection, "build.finished"), told, None)


async def test_a_watch_stops_when_the_server_ends_it(setup: _Setup) -> None:
    events, told = _events(setup), _Told()
    connection = await _open_to(setup, setup.one)
    watching = asyncio.create_task(
        events.watch(_watch(setup.one, connection, "issue.opened", repo="owner/one"), told, None)
    )
    await _asked(setup.served.server, "events/stream")
    await setup.served.server.terminate()

    with pytest.raises(WatchFailed, match="The server ended the watch: Forbidden: Access revoked"):
        async with asyncio.timeout(5):
            await watching


_HOOK_KINDS = [{**KINDS[0], "name": "issue.posted", "delivery": ["webhook", "poll"]}]
_HOOK = "https://cortex.example.com/hooks/w_issue.posted"


class _Hooked(SimpleNamespace):
    setup: _Setup
    events: McpEvents
    told: _Told
    watch: McpWatch
    answers: list[int]


@pytest.fixture
async def hooked() -> AsyncGenerator[_Hooked]:
    made = _Hooked(answers=[], told=_Told())

    async def post(url: str, headers: Mapping[str, str], body: bytes) -> int:
        # The webhook route of a watch, as the backend would have it.
        assert url == _HOOK
        believed = await made.events.posted(made.watch, headers, body, made.told)
        made.answers.append(200 if believed else 401)
        return made.answers[-1]

    server = EventsServer(kinds=_HOOK_KINDS, post=post, allow_http=False, max_ttl_ms=300)
    async for setup in _setup(server):
        made.setup = setup
        made.events = _events(setup)
        connection = await _open_to(setup, setup.one)
        made.watch = _watch(setup.one, connection, "issue.posted", repo="owner/one")
        yield made


async def test_a_watch_by_webhook_is_renewed_believed_and_stopped(hooked: _Hooked) -> None:
    server = hooked.setup.served.server
    delivered = _Deliveries()
    watching = asyncio.create_task(
        hooked.events.watch(hooked.watch, hooked.told, _HOOK, on_delivery=delivered)
    )
    await _asked(server, "events/subscribe")

    await server.emit("issue.posted", {"repo": "owner/one", "number": 1}, event_id="p1")
    await hooked.told.wait_for(1)
    # A grant lasts 300 ms, and is asked for again when four fifths of it have gone. By
    # the third ask, 480 ms or more have passed, so the first grant has lapsed: the
    # server drops a lapsed subscription, and a post still coming shows it was renewed.
    await _asked(server, "events/subscribe", times=3)
    await server.emit("issue.posted", {"repo": "owner/one", "number": 2}, event_id="p2")
    await hooked.told.wait_for(2)
    await _stop(watching)

    assert [(name, event_id, cursor) for name, _, event_id, _, cursor in hooked.told.events] == [
        ("issue.posted", "p1", "1"),
        ("issue.posted", "p2", "2"),
    ]
    assert hooked.answers == [200, 200]
    assert delivered.seen == ["webhook"]
    # Stopped when the watch ends.
    assert server.asked[-1] == "events/unsubscribe"
    assert server.hooks == {}


async def test_a_post_not_signed_for_the_watch_is_refused(hooked: _Hooked) -> None:
    server = hooked.setup.served.server
    watching = asyncio.create_task(hooked.events.watch(hooked.watch, hooked.told, _HOOK))
    await _asked(server, "events/subscribe")
    [hook] = server.hooks.values()
    hook.secret = "whsec_" + base64.b64encode(b"z" * 32).decode()

    await server.emit("issue.posted", {"repo": "owner/one", "number": 1})
    other = dataclasses.replace(hooked.watch, watch_id="w_other")
    [(message_id, _)] = server.posts
    await _stop(watching)

    assert hooked.answers == [401]
    assert hooked.told.events == []
    assert message_id.startswith("msg_")
    assert await hooked.events.posted(other, {}, b"{}", hooked.told) is False


async def test_a_server_that_will_not_post_to_the_address_is_polled(setup: _Setup) -> None:
    setup.served.server.next_poll_ms = 10
    setup.served.server.kinds = _HOOK_KINDS
    setup.served.server.allow_http = False
    events, told = _events(setup), _Told()
    connection = await _open_to(setup, setup.one)
    watch = _watch(setup.one, connection, "issue.posted", repo="owner/one")
    delivered = _Deliveries()
    watching = asyncio.create_task(
        events.watch(watch, told, "http://plain.example.com/hook", on_delivery=delivered)
    )
    await _asked(setup.served.server, "events/poll")

    await setup.served.server.emit("issue.posted", {"repo": "owner/one", "number": 1})
    await told.wait_for(1)
    await _stop(watching)

    assert setup.served.server.asked[:3] == ["events/list", "events/subscribe", "events/poll"]
    # The server never took the webhook, so the watch only says it polls.
    assert delivered.seen == ["poll"]


async def _asked(server: EventsServer, method: str, times: int = 1) -> None:
    """Wait until the server has been asked `method`, this many times."""
    async with asyncio.timeout(5):
        while server.asked.count(method) < times:
            server.heard.clear()
            await server.heard.wait()


async def _stop(watching: asyncio.Task[None]) -> None:
    """End a watch, as removing it does, and wait for it to have ended."""
    watching.cancel()
    with pytest.raises(asyncio.CancelledError):
        await watching


async def test_a_watch_tells_where_it_has_got_to_when_no_event_comes(setup: _Setup) -> None:
    setup.served.server.next_poll_ms = 10
    events, told = _events(setup), _Told()
    connection = await _open_to(setup, setup.one)
    moved: list[str] = []

    async def on_cursor(cursor: str) -> None:
        moved.append(cursor)

    watching = asyncio.create_task(
        events.watch(_watch(setup.one, connection, "build.finished"), told, None, on_cursor)
    )
    await _asked(setup.served.server, "events/poll")
    await asyncio.sleep(0.1)
    await _stop(watching)

    # The first poll from now gives a place: it is told once, though asked from again.
    assert moved == ["0"]
    assert told.events == []


async def test_a_watch_whose_server_does_not_answer_is_failing(
    setup: _Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    events, told = _events(setup), _Told()
    connection = await _open_to(setup, setup.one)

    async def unreachable(*args: Any, **kwargs: Any) -> Any:
        raise McpFailure("the server could not be reached")

    monkeypatch.setattr(setup.mcp, "poll_events", unreachable)

    with pytest.raises(WatchFailed, match="does not answer: the server could not be reached"):
        async with asyncio.timeout(5):
            await events.watch(_watch(setup.one, connection, "build.finished"), told, None)
