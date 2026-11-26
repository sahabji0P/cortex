"""MCP events (SEP-3415) through the real adapter, over HTTP, against a real server.

The server is the one in `tests/mcp_events_server.py`, run on a port of this machine,
and reached through the SDK's own client with nothing faked between them.
"""

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Sequence

import pytest
from mcp import Client
from mcp.client import ClientExtension
from mcp.server.mcpserver import MCPServer

from app.adapters.mcp_adp import (
    McpAdapterClass,
    McpEventStream,
    McpEventType,
    McpFailure,
    McpOccurrence,
    McpServer,
    McpStreamMark,
)
from tests.mcp_events_server import EventsServer, Running, running

_REPO = {"repo": "owner/one"}


@pytest.fixture
async def served() -> AsyncGenerator[Running]:
    async with running(EventsServer()) as server:
        yield server


@pytest.fixture
async def mcp() -> AsyncGenerator[McpAdapterClass]:
    adapter = McpAdapterClass()
    yield adapter
    await adapter.close()


async def test_a_servers_kinds_of_event_are_listed_with_its_tools(
    served: Running, mcp: McpAdapterClass
) -> None:
    listed = await mcp.list_tools(McpServer(served.url))

    assert listed.events == [
        McpEventType(
            name="issue.opened",
            description="Fires when an issue is opened in a repo",
            delivery=("poll", "push", "webhook"),
            input_schema={
                "type": "object",
                "properties": {"repo": {"type": "string", "description": "owner/name"}},
                "required": ["repo"],
            },
        ),
        McpEventType(
            name="build.finished",
            description="Fires when a build ends",
            delivery=("poll",),
            input_schema={"type": "object", "properties": {}},
        ),
    ]
    assert served.server.asked == ["events/list"]


async def test_a_server_without_the_extension_has_no_events() -> None:
    plain = MCPServer("plain")

    @plain.tool()
    async def ping() -> str:
        """Say pong."""
        return "pong"

    @contextlib.asynccontextmanager
    async def opener(_: McpServer, extensions: Sequence[ClientExtension]) -> AsyncGenerator[Client]:
        async with Client(plain, extensions=extensions) as client:
            yield client

    mcp = McpAdapterClass(opener)
    listed = await mcp.list_tools(McpServer("https://plain.example.com/mcp"))
    await mcp.close()

    assert [tool.name for tool in listed.tools] == [ping.__name__]
    assert listed.events == []


async def test_a_poll_from_now_has_nothing_and_says_where_to_ask_from(
    served: Running, mcp: McpAdapterClass
) -> None:
    await served.server.emit("issue.opened", _REPO)

    polled = await mcp.poll_events(McpServer(served.url), "issue.opened", _REPO, None)

    assert polled.events == []
    assert polled.cursor == "1"
    assert (polled.truncated, polled.has_more, polled.next_poll_ms) == (False, False, 1_000)


async def test_a_poll_from_a_cursor_has_what_happened_since(
    served: Running, mcp: McpAdapterClass
) -> None:
    server = McpServer(served.url)
    start = await mcp.poll_events(server, "issue.opened", _REPO, None)
    opened = await served.server.emit("issue.opened", {**_REPO, "number": 7}, event_id="evt_7")
    await served.server.emit("issue.opened", {"repo": "owner/two", "number": 8})

    polled = await mcp.poll_events(server, "issue.opened", _REPO, start.cursor)

    assert [(one.event_id, one.name, one.data, one.cursor) for one in polled.events] == [
        (opened, "issue.opened", {**_REPO, "number": 7}, None)
    ]
    assert polled.events[0].at is not None
    assert polled.cursor == "2"
    again = await mcp.poll_events(server, "issue.opened", _REPO, polled.cursor)
    assert again.events == []


async def test_a_poll_says_when_events_were_skipped(served: Running, mcp: McpAdapterClass) -> None:
    for number in range(3):
        await served.server.emit("issue.opened", {**_REPO, "number": number})
    served.server.forget_before(2)

    polled = await mcp.poll_events(McpServer(served.url), "issue.opened", _REPO, "0")

    assert polled.truncated
    assert [one.data["number"] for one in polled.events] == [2]


async def test_a_poll_of_a_kind_the_server_does_not_serve_is_refused(
    served: Running, mcp: McpAdapterClass
) -> None:
    with pytest.raises(McpFailure) as failed:
        await mcp.poll_events(McpServer(served.url), "nothing.happened", {}, None)

    assert failed.value.code == -32023
    assert not failed.value.sign_in_needed


async def _next(stream: McpEventStream) -> McpOccurrence | McpStreamMark:
    async with asyncio.timeout(5):
        return await anext(aiter(stream))


async def test_a_stream_sends_each_event_as_it_happens(
    served: Running, mcp: McpAdapterClass
) -> None:
    await served.server.emit("issue.opened", _REPO)
    stream = await mcp.open_stream(McpServer(served.url), "issue.opened", _REPO, None)
    try:
        assert stream.start == McpStreamMark("1")
        assert stream.heartbeat_ms == 30_000
        await served.server.emit("issue.opened", {"repo": "owner/two"})
        opened = await served.server.emit("issue.opened", {**_REPO, "number": 3})

        got = await _next(stream)

        assert isinstance(got, McpOccurrence)
        assert (got.event_id, got.data, got.cursor) == (opened, {**_REPO, "number": 3}, "3")
        assert served.server.asked == ["events/stream"]
    finally:
        await stream.aclose()
    # Closing it aborts the request, which ends it at the server.
    async with asyncio.timeout(5):
        await served.server.stream_closed.wait()
    assert not served.server.streams


async def test_a_streams_events_come_in_order_however_many(
    served: Running, mcp: McpAdapterClass
) -> None:
    stream = await mcp.open_stream(McpServer(served.url), "issue.opened", _REPO, None)
    for number in range(300):
        await served.server.emit("issue.opened", {**_REPO, "number": number})

    numbers: list[object] = []
    while len(numbers) < 300:
        got = await _next(stream)
        if isinstance(got, McpOccurrence):
            numbers.append(got.data["number"])
    await stream.aclose()

    assert numbers == list(range(300))


async def test_a_quiet_stream_sends_where_it_has_got_to() -> None:
    async with running(EventsServer(heartbeat_ms=50)) as served:
        mcp = McpAdapterClass()
        stream = await mcp.open_stream(McpServer(served.url), "issue.opened", _REPO, None)
        await served.server.emit("issue.opened", {"repo": "owner/two"})

        assert await _next(stream) == McpStreamMark("1")
        await mcp.close()


async def test_a_stream_that_goes_silent_is_dropped() -> None:
    async with running(EventsServer(heartbeat_ms=50)) as served:
        mcp = McpAdapterClass()
        stream = await mcp.open_stream(McpServer(served.url), "issue.opened", _REPO, None)
        served.server.mute = True

        with pytest.raises(McpFailure, match="said nothing"):
            await _next(stream)
        await mcp.close()


async def test_a_dropped_stream_raises_and_is_taken_up_again_from_its_cursor(
    served: Running, mcp: McpAdapterClass
) -> None:
    server = McpServer(served.url)
    stream = await mcp.open_stream(server, "issue.opened", _REPO, None)
    await served.server.emit("issue.opened", {**_REPO, "number": 1})
    first = await _next(stream)
    assert isinstance(first, McpOccurrence)

    served.server.drop()
    with pytest.raises(McpFailure, match="closed the stream"):
        await _next(stream)
    await stream.aclose()
    # What happens while it is down is sent when it comes back from its cursor.
    await served.server.emit("issue.opened", {**_REPO, "number": 2})
    again = await mcp.open_stream(server, "issue.opened", _REPO, first.cursor)
    got = await _next(again)
    await again.aclose()

    assert again.start == McpStreamMark("1")
    assert isinstance(got, McpOccurrence)
    assert got.data["number"] == 2


async def test_a_stream_from_a_cursor_too_old_says_events_were_skipped(
    served: Running, mcp: McpAdapterClass
) -> None:
    for number in range(3):
        await served.server.emit("issue.opened", {**_REPO, "number": number})
    served.server.forget_before(2)

    stream = await mcp.open_stream(McpServer(served.url), "issue.opened", _REPO, "0")
    got = await _next(stream)
    await stream.aclose()

    assert stream.start == McpStreamMark("2", truncated=True)
    assert isinstance(got, McpOccurrence)
    assert got.data["number"] == 2


async def test_a_stream_ends_when_the_server_ends_the_subscription(
    served: Running, mcp: McpAdapterClass
) -> None:
    stream = await mcp.open_stream(McpServer(served.url), "issue.opened", _REPO, None)
    await served.server.emit("issue.opened", {**_REPO, "number": 1})
    await served.server.terminate()

    got = [item async for item in stream]
    await stream.aclose()

    assert [item.data["number"] for item in got if isinstance(item, McpOccurrence)] == [1]
    assert stream.ended == "Forbidden: Access revoked"


async def test_a_stream_of_a_kind_not_sent_that_way_is_refused(
    served: Running, mcp: McpAdapterClass
) -> None:
    with pytest.raises(McpFailure) as failed:
        await mcp.open_stream(McpServer(served.url), "build.finished", {}, None)

    assert failed.value.code == -32026
    assert not mcp._streams  # pyright: ignore[reportPrivateUsage]
