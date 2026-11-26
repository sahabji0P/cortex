"""MCP events (SEP-3415) through the real adapter, over HTTP, against a real server.

The server is the one in `tests/mcp_events_server.py`, run on a port of this machine,
and reached through the SDK's own client with nothing faked between them.
"""

import asyncio
import base64
import contextlib
import json
import time
from collections.abc import AsyncGenerator, Mapping, Sequence
from datetime import UTC, datetime

import pytest
from mcp import Client
from mcp.client import ClientExtension
from mcp.server.mcpserver import MCPServer

from app.adapters.mcp_adp import (
    McpAdapterClass,
    McpEnded,
    McpEventStream,
    McpEventType,
    McpFailure,
    McpMark,
    McpOccurrence,
    McpPost,
    McpServer,
    McpVerification,
)
from tests.mcp_events_server import EventsServer, Running, running, sign

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


async def _next(stream: McpEventStream) -> McpOccurrence | McpMark:
    async with asyncio.timeout(5):
        return await anext(aiter(stream))


async def test_a_stream_sends_each_event_as_it_happens(
    served: Running, mcp: McpAdapterClass
) -> None:
    await served.server.emit("issue.opened", _REPO)
    stream = await mcp.open_stream(McpServer(served.url), "issue.opened", _REPO, None)
    try:
        assert stream.start == McpMark("1")
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

        assert await _next(stream) == McpMark("1")
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

    assert again.start == McpMark("1")
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

    assert stream.start == McpMark("2", truncated=True)
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


_HOOK = "https://hooks.example.com/w/one"
_SECRET = "whsec_" + base64.b64encode(b"k" * 32).decode()


class _Hooks:
    """The address webhook posts go to: it keeps each one, and answers with `status`."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, str], bytes]] = []
        self.status = 200

    async def post(self, url: str, headers: Mapping[str, str], body: bytes) -> int:
        self.posts.append((url, dict(headers), body))
        return self.status


@pytest.fixture
async def hooked() -> AsyncGenerator[tuple[Running, _Hooks]]:
    hooks = _Hooks()
    async with running(EventsServer(post=hooks.post, allow_http=False, token="t0k")) as served:
        yield served, hooks


def _signed_in(url: str) -> McpServer:
    return McpServer(url, (("Authorization", "Bearer t0k"),))


async def test_a_webhook_is_posted_each_event_signed_with_our_secret(
    hooked: tuple[Running, _Hooks], mcp: McpAdapterClass
) -> None:
    served, hooks = hooked
    granted = await mcp.subscribe_events(
        _signed_in(served.url),
        "issue.opened",
        _REPO,
        url=_HOOK,
        secret=_SECRET,
        cursor=None,
        ttl_ms=60_000,
    )
    opened = await served.server.emit("issue.opened", {**_REPO, "number": 5})
    await served.server.emit("issue.opened", {"repo": "owner/two"})

    assert granted.id.startswith("sub_")
    assert granted.refresh_before is not None
    assert 50 < (granted.refresh_before - datetime.now(UTC)).total_seconds() <= 60
    assert (granted.cursor, granted.truncated, granted.delivering) == ("0", False, None)
    [(url, headers, body)] = hooks.posts
    assert url == _HOOK
    post = mcp.check_post(_SECRET, headers, body)
    assert post is not None
    assert post.subscription_id == granted.id
    assert post.message_id == headers["webhook-id"]
    assert isinstance(post.item, McpOccurrence)
    assert (post.item.event_id, post.item.data, post.item.cursor) == (
        opened,
        {**_REPO, "number": 5},
        "1",
    )


async def test_a_post_not_signed_with_our_secret_is_not_believed(
    hooked: tuple[Running, _Hooks], mcp: McpAdapterClass
) -> None:
    served, hooks = hooked
    await mcp.subscribe_events(
        _signed_in(served.url),
        "issue.opened",
        _REPO,
        url=_HOOK,
        secret=_SECRET,
        cursor=None,
        ttl_ms=60_000,
    )
    await served.server.emit("issue.opened", _REPO)
    [(_, headers, body)] = hooks.posts
    other = "whsec_" + base64.b64encode(b"x" * 32).decode()
    stamp = int(headers["webhook-timestamp"])

    assert mcp.check_post(_SECRET, headers, body) is not None
    assert mcp.check_post(other, headers, body) is None
    assert mcp.check_post(_SECRET, headers, body.replace(b"one", b"two")) is None
    assert mcp.check_post(_SECRET, {**headers, "webhook-id": "msg_other"}, body) is None
    assert mcp.check_post(_SECRET, {**headers, "webhook-signature": ""}, body) is None
    late = datetime.fromtimestamp(stamp + 301, UTC)
    assert mcp.check_post(_SECRET, headers, body, now=late) is None
    # While a secret changes, a post may carry a signature with each: one is enough.
    both = {**headers, "webhook-signature": f"v1,bm9wZQ== {headers['webhook-signature']}"}
    assert mcp.check_post(_SECRET, both, body) is not None


async def test_a_webhook_is_renewed_by_asking_again_and_stopped_by_unsubscribing(
    hooked: tuple[Running, _Hooks], mcp: McpAdapterClass
) -> None:
    served, hooks = hooked
    server = _signed_in(served.url)
    first = await mcp.subscribe_events(
        server, "issue.opened", _REPO, url=_HOOK, secret=_SECRET, cursor=None, ttl_ms=1_000
    )
    await served.server.emit("issue.opened", _REPO)
    again = await mcp.subscribe_events(
        server, "issue.opened", _REPO, url=_HOOK, secret=_SECRET, cursor="1", ttl_ms=60_000
    )

    assert again.id == first.id
    assert first.refresh_before is not None and again.refresh_before is not None
    assert again.refresh_before > first.refresh_before
    assert again.cursor == "1"
    await mcp.unsubscribe_events(server, "issue.opened", _REPO, _HOOK)
    await served.server.emit("issue.opened", _REPO)
    assert len(hooks.posts) == 1
    with pytest.raises(McpFailure) as failed:
        await mcp.unsubscribe_events(server, "issue.opened", _REPO, _HOOK)
    assert failed.value.code == -32023


async def test_a_webhook_is_refused_without_a_pass_or_at_a_plain_address(
    hooked: tuple[Running, _Hooks], mcp: McpAdapterClass
) -> None:
    served, _ = hooked
    with pytest.raises(McpFailure) as unsigned:
        await mcp.subscribe_events(
            McpServer(served.url),
            "issue.opened",
            _REPO,
            url=_HOOK,
            secret=_SECRET,
            cursor=None,
            ttl_ms=60_000,
        )
    with pytest.raises(McpFailure) as plain:
        await mcp.subscribe_events(
            _signed_in(served.url),
            "issue.opened",
            _REPO,
            url="http://hooks.example.com/w",
            secret=_SECRET,
            cursor=None,
            ttl_ms=60_000,
        )

    assert unsigned.value.code == -32024
    assert plain.value.code == -32602


async def test_a_webhook_is_told_when_its_subscription_ends(
    hooked: tuple[Running, _Hooks], mcp: McpAdapterClass
) -> None:
    served, hooks = hooked
    await mcp.subscribe_events(
        _signed_in(served.url),
        "issue.opened",
        _REPO,
        url=_HOOK,
        secret=_SECRET,
        cursor=None,
        ttl_ms=60_000,
    )
    await served.server.terminate()

    [(_, headers, body)] = hooks.posts
    post = mcp.check_post(_SECRET, headers, body)
    assert post is not None
    assert post.item == McpEnded("Forbidden: Access revoked")
    assert post.message_id.startswith("msg_terminated_")


def test_the_servers_own_posts_are_read(mcp: McpAdapterClass) -> None:
    def signed(body: dict[str, object]) -> McpPost | None:
        raw = json.dumps(body).encode()
        stamp = str(int(time.time()))
        signature = sign(_SECRET, "msg_x", stamp, raw)
        headers = {
            "webhook-id": "msg_x",
            "webhook-timestamp": stamp,
            "webhook-signature": f"v1,{signature}",
        }
        return mcp.check_post(_SECRET, headers, raw)

    gap = signed({"type": "gap", "cursor": "9"})
    check = signed({"type": "verification", "challenge": "n0nce"})

    assert gap is not None and gap.item == McpMark("9", truncated=True)
    assert gap.subscription_id is None
    assert check is not None and check.item == McpVerification("n0nce")
    assert signed({"type": "something.else"}) is None
    assert signed({"eventId": "e1"}) is None  # an event with no name


def test_a_webhook_secret_is_made_as_standard_webhooks_writes_one(mcp: McpAdapterClass) -> None:
    assert mcp.webhook_secret(b"k" * 32) == _SECRET
    with pytest.raises(ValueError):
        mcp.webhook_secret(b"short")


async def test_a_server_that_refuses_the_pass_says_a_sign_in_is_needed(
    served: Running, mcp: McpAdapterClass
) -> None:
    served.server.pass_needed = "the-right-one"
    server = McpServer(served.url, token="an-old-one")

    with pytest.raises(McpFailure) as streamed:
        await mcp.open_stream(server, "issue.opened", _REPO, None)
    with pytest.raises(McpFailure) as polled:
        await mcp.poll_events(server, "issue.opened", _REPO, None)

    assert streamed.value.sign_in_needed
    assert polled.value.sign_in_needed
    stream = await mcp.open_stream(
        McpServer(served.url, token="the-right-one"), "issue.opened", _REPO, None
    )
    await stream.aclose()
