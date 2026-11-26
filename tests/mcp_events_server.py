"""
An MCP server that serves events (SEP-3415, `io.modelcontextprotocol/events`), for tests.

The released reference server serves none, so this one is ours. It is a real MCP server,
made with the SDK's server side and reached over Streamable HTTP on a local port, as a
real server is. It lists two kinds of event, serves all three deliveries (poll, stream
and webhook), keeps every event it has sent so a cursor can replay from any point, and
is told to send one with `emit`. A test can also cut every open stream (`drop`), end
every subscription (`terminate`), or forget what is older than a point, so a cursor
falls out of what it keeps (`forget_before`).

Everything is in memory. A cursor is the number of events sent before it, as text.

Two things the spec asks of a real server are relaxed, and say so: a webhook's address
may be plain `http` on this machine when `allow_http` is on, and a webhook address is
taken as verified (the spec's allowlist), so no challenge is sent to it. A subscription
needs a pass only when `token` is set.

`scripts/mcp_events_server.py` runs it on a port of its own.
"""

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx2
import uvicorn
from mcp.server.context import ServerRequestContext
from mcp.server.extension import Extension, MethodBinding
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp_types import Notification, RequestParams, ServerNotification
from pydantic import ConfigDict
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

__all__ = ["EVENTS", "KINDS", "EventsServer", "Posted", "Running", "running", "sign"]

EVENTS = "io.modelcontextprotocol/events"
_SUBSCRIPTION_ID = "io.modelcontextprotocol/subscriptionId"

# The spec's error codes.
_INVALID_PARAMS = -32602
_NOT_FOUND = -32023
_FORBIDDEN = -32024
_UNSUPPORTED = -32026

# The kinds of event served: an issue opened in a repo, and a build that ended.
KINDS: list[dict[str, Any]] = [
    {
        "name": "issue.opened",
        "description": "Fires when an issue is opened in a repo",
        "delivery": ["poll", "push", "webhook"],
        "inputSchema": {
            "type": "object",
            "properties": {"repo": {"type": "string", "description": "owner/name"}},
            "required": ["repo"],
        },
        "payloadSchema": {
            "type": "object",
            "properties": {"repo": {"type": "string"}, "number": {"type": "integer"}},
        },
    },
    {
        "name": "build.finished",
        "description": "Fires when a build ends",
        "delivery": ["poll"],
        "inputSchema": {"type": "object", "properties": {}},
        "payloadSchema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
    },
]

# What a webhook post is answered with, by the address it went to: the status.
type Posted = Callable[[str, Mapping[str, str], bytes], Awaitable[int]]


class _Params(RequestParams):
    model_config = ConfigDict(extra="allow")


@dataclass(slots=True)
class _Event:
    event_id: str
    name: str
    timestamp: str
    data: dict[str, Any]


@dataclass(slots=True)
class _Hook:
    """A webhook subscription."""

    id: str
    name: str
    arguments: dict[str, Any]
    url: str
    secret: str
    refresh_before: datetime
    # Where its deliveries have got to: the next event to send it.
    next: int


@dataclass(slots=True, eq=False)
class _Stream:
    """An open `events/stream`: what wakes it, and whether it is to be cut or ended."""

    wake: asyncio.Event = field(default_factory=asyncio.Event)
    cut: bool = False
    ended: dict[str, Any] | None = None


class EventsServer:
    """The server's state, and the MCP server that serves it."""

    def __init__(
        self,
        *,
        allow_http: bool = True,
        token: str | None = None,
        post: Posted | None = None,
        heartbeat_ms: int = 30_000,
        next_poll_ms: int = 1_000,
        max_ttl_ms: int = 3_600_000,
        kinds: list[dict[str, Any]] | None = None,
    ) -> None:
        # The kinds of event it lists and serves.
        self.kinds = KINDS if kinds is None else kinds
        self.allow_http = allow_http
        self.token = token
        self.heartbeat_ms = heartbeat_ms
        self.next_poll_ms = next_poll_ms
        self.max_ttl_ms = max_ttl_ms
        # When set, an open stream sends no heartbeat: it goes quiet, as a dead one does.
        self.mute = False
        # When set, every request without this pass is answered 401, as a server whose
        # pass has run out answers.
        self.pass_needed: str | None = None
        self.events: list[_Event] = []
        # Events before this one are no longer kept: a cursor before it is a gap.
        self.floor = 0
        self.hooks: dict[str, _Hook] = {}
        self.streams: set[_Stream] = set()
        # Set each time a stream ends at the server, however it ends.
        self.stream_closed = asyncio.Event()
        # Every request asked of it, by method, and the webhook posts it made.
        self.asked: list[str] = []
        # Set each time it is asked something.
        self.heard = asyncio.Event()
        self.posts: list[tuple[str, int]] = []
        self._post = _post_over_http if post is None else post
        self.mcp = MCPServer("events", extensions=[_Events(self)])

    def app(self) -> Any:
        """The server as an ASGI app, at `/mcp`."""
        return self.mcp.streamable_http_app(
            stateless_http=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )

    # ---- what a test does

    async def emit(self, name: str, data: dict[str, Any], *, event_id: str | None = None) -> str:
        """Send an event: to every open stream, and to every webhook it matches. Its id."""
        event = _Event(
            event_id=event_id or f"evt_{secrets.token_hex(6)}",
            name=name,
            timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            data=data,
        )
        self.events.append(event)
        for stream in self.streams:
            stream.wake.set()
        await self.deliver()
        return event.event_id

    def drop(self) -> None:
        """Cut every open stream, as a server restarting does: no `terminated` is sent."""
        for stream in self.streams:
            stream.cut = True
            stream.wake.set()

    async def terminate(self, reason: str = "Access revoked") -> None:
        """End every subscription: open streams, and webhooks with a signed envelope."""
        error = {"code": _FORBIDDEN, "message": "Forbidden", "data": {"reason": reason}}
        for stream in self.streams:
            stream.ended = error
            stream.wake.set()
        for hook in list(self.hooks.values()):
            await self._send(hook, {"type": "terminated", "error": error}, "msg_terminated_")
        self.hooks.clear()

    def forget_before(self, position: int) -> None:
        """Keep no event before this position: a cursor older than it has a gap."""
        self.floor = position

    async def deliver(self) -> None:
        """Post what each live webhook subscription has not been sent yet."""
        now = datetime.now(UTC)
        for hook in list(self.hooks.values()):
            if hook.refresh_before <= now:
                del self.hooks[hook.id]  # it lapsed: nobody refreshed it
                continue
            while hook.next < len(self.events):
                event = self.events[hook.next]
                if _matches(event, hook.name, hook.arguments):
                    body = _occurrence(event, str(hook.next + 1))
                    status = await self._send(hook, body, "msg_")
                    if status >= 300 and status != 410:
                        break  # tried again on the next delivery
                hook.next += 1

    # ---- the methods

    def list_events(self) -> dict[str, Any]:
        return {"resultType": "complete", "events": self.kinds}

    def poll(self, params: dict[str, Any]) -> dict[str, Any]:
        name = self._kind(params, "poll")
        cursor = params.get("cursor")
        if cursor is None:
            return self._batch([], len(self.events), truncated=False)
        start, truncated = self._start(cursor)
        limit = int(params.get("maxEvents") or 50)
        found: list[dict[str, Any]] = []
        position = start
        while position < len(self.events) and len(found) < limit:
            event = self.events[position]
            position += 1
            if _matches(event, name, _arguments(params)):
                found.append(_occurrence(event, None))
        return self._batch(found, position, truncated=truncated)

    async def stream(self, ctx: ServerRequestContext[Any, Any], params: dict[str, Any]) -> Any:
        name = self._kind(params, "push")
        arguments = _arguments(params)
        sub = {_SUBSCRIPTION_ID: ctx.request_id}
        cursor = params.get("cursor")
        position, truncated = (len(self.events), False) if cursor is None else self._start(cursor)
        stream = _Stream()
        self.streams.add(stream)

        async def say(method: str, body: dict[str, Any]) -> None:
            note = Notification[dict[str, Any], str](method=method, params={**body, "_meta": sub})
            await ctx.session.send_notification(
                cast("ServerNotification", note), related_request_id=ctx.request_id
            )

        try:
            await say(
                "notifications/events/active",
                {
                    "cursor": str(position),
                    "truncated": truncated,
                    "heartbeatMs": self.heartbeat_ms,
                },
            )
            while True:
                while position < len(self.events):
                    event = self.events[position]
                    position += 1
                    if _matches(event, name, arguments):
                        await say("notifications/events/event", _occurrence(event, str(position)))
                if stream.ended is not None:
                    await say("notifications/events/terminated", {"error": stream.ended})
                    break
                if stream.cut:
                    break
                stream.wake.clear()
                try:
                    async with asyncio.timeout(self.heartbeat_ms / 1000):
                        await stream.wake.wait()
                except TimeoutError:
                    if not self.mute:
                        await say("notifications/events/heartbeat", {"cursor": str(position)})
        finally:
            self.streams.discard(stream)
            self.stream_closed.set()
        return {"resultType": "complete", "_meta": sub}

    def subscribe(self, request: Request | None, params: dict[str, Any]) -> dict[str, Any]:
        self._signed_in(request)
        name = self._kind(params, "webhook")
        delivery = _object(params.get("delivery"))
        url, secret = delivery.get("url"), delivery.get("secret")
        if not isinstance(url, str) or not (
            url.startswith("https://") or (self.allow_http and url.startswith("http://"))
        ):
            raise MCPError(_INVALID_PARAMS, "the callback address has to be https")
        if not isinstance(secret, str) or not _good_secret(secret):
            raise MCPError(_INVALID_PARAMS, "the secret is not a whsec_ secret")
        arguments = _arguments(params)
        key = _hook_id(url, name, arguments)
        ttl = params.get("ttlMs")
        ttl = self.max_ttl_ms if not isinstance(ttl, int) else min(ttl, self.max_ttl_ms)
        refresh_before = datetime.now(UTC) + timedelta(milliseconds=ttl)
        cursor = params.get("cursor")
        hook = self.hooks.get(key)
        truncated = False
        if hook is None:
            start, truncated = (len(self.events), False) if cursor is None else self._start(cursor)
            hook = _Hook(key, name, arguments, url, secret, refresh_before, start)
            self.hooks[key] = hook
        else:
            hook.secret = secret
            hook.refresh_before = refresh_before
        return {
            "resultType": "complete",
            "id": key,
            "refreshBefore": refresh_before.isoformat().replace("+00:00", "Z"),
            "cursor": str(hook.next),
            "truncated": truncated,
        }

    def unsubscribe(self, request: Request | None, params: dict[str, Any]) -> dict[str, Any]:
        self._signed_in(request)
        name = params.get("name")
        url = _object(params.get("delivery")).get("url")
        key = _hook_id(str(url), str(name), _arguments(params))
        if self.hooks.pop(key, None) is None:
            raise MCPError(_NOT_FOUND, "no such subscription", {"kind": "subscription"})
        return {"resultType": "complete"}

    # ---- helpers

    def _kind(self, params: dict[str, Any], delivery: str) -> str:
        name = params.get("name")
        kind = next((kind for kind in self.kinds if kind["name"] == name), None)
        if kind is None:
            raise MCPError(_NOT_FOUND, f"no event called {name}", {"kind": "event"})
        if delivery not in kind["delivery"]:
            raise MCPError(
                _UNSUPPORTED, "not offered", {"feature": "deliveryMode", "value": delivery}
            )
        if "repo" in kind["inputSchema"].get("required", []) and not isinstance(
            _arguments(params).get("repo"), str
        ):
            raise MCPError(_INVALID_PARAMS, "a repo is needed")
        return cast("str", name)

    def _start(self, cursor: str) -> tuple[int, bool]:
        """Where a cursor starts from, and whether events were skipped to start there."""
        try:
            position = int(cursor)
        except ValueError:
            return len(self.events), True
        if position < self.floor:
            return self.floor, True
        return min(position, len(self.events)), False

    def _batch(self, events: list[dict[str, Any]], position: int, *, truncated: bool) -> Any:
        return {
            "resultType": "complete",
            "events": events,
            "cursor": str(position),
            "truncated": truncated,
            "hasMore": position < len(self.events),
            "nextPollMs": self.next_poll_ms,
        }

    def _signed_in(self, request: Request | None) -> None:
        if self.token is None:
            return
        given = None if request is None else request.headers.get("authorization")
        if given != f"Bearer {self.token}":
            raise MCPError(_FORBIDDEN, "a webhook needs a signed-in caller")

    async def _send(self, hook: _Hook, body: dict[str, Any], prefix: str) -> int:
        raw = json.dumps(body).encode()
        message_id = f"{prefix}{secrets.token_hex(8)}"
        stamp = str(int(time.time()))
        headers = {
            "content-type": "application/json",
            "webhook-id": message_id,
            "webhook-timestamp": stamp,
            "webhook-signature": f"v1,{sign(hook.secret, message_id, stamp, raw)}",
            "x-mcp-subscription-id": hook.id,
        }
        try:
            status = await self._post(hook.url, headers, raw)
        except httpx2.HTTPError:
            status = 599
        self.posts.append((message_id, status))
        return status


class _Events(Extension):
    """The events extension, as this server serves it."""

    identifier = EVENTS

    def __init__(self, server: EventsServer) -> None:
        self._server = server

    def methods(self) -> list[MethodBinding]:
        server = self._server

        def asked(
            method: str, answer: Callable[[ServerRequestContext[Any, Any], dict[str, Any]], Any]
        ) -> MethodBinding:
            async def handle(ctx: ServerRequestContext[Any, Any], params: _Params) -> Any:
                server.asked.append(method)
                server.heard.set()
                result = answer(ctx, params.model_dump(by_alias=True, exclude={"meta"}))
                return await result if asyncio.iscoroutine(result) else result

            return MethodBinding(method, _Params, handle)

        return [
            asked("events/list", lambda ctx, params: server.list_events()),
            asked("events/poll", lambda ctx, params: server.poll(params)),
            asked("events/stream", server.stream),
            asked("events/subscribe", lambda ctx, params: server.subscribe(ctx.request, params)),
            asked(
                "events/unsubscribe", lambda ctx, params: server.unsubscribe(ctx.request, params)
            ),
        ]


def sign(secret: str, message_id: str, stamp: str, body: bytes) -> str:
    """A Standard Webhooks signature, without its `v1,`."""
    key = base64.b64decode(secret.removeprefix("whsec_"))
    signed = f"{message_id}.{stamp}.".encode() + body
    return base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()


def _good_secret(secret: str) -> bool:
    if not secret.startswith("whsec_"):
        return False
    try:
        key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
    except ValueError:
        return False
    return 24 <= len(key) <= 64


def _object(value: Any) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _arguments(params: Mapping[str, Any]) -> dict[str, Any]:
    return _object(params.get("arguments"))


def _hook_id(url: str, name: str, arguments: Mapping[str, Any]) -> str:
    canonical = json.dumps([url, name, arguments], sort_keys=True, separators=(",", ":"))
    return f"sub_{hashlib.sha256(canonical.encode()).hexdigest()[:16]}"


def _matches(event: _Event, name: str, arguments: Mapping[str, Any]) -> bool:
    """Whether an event is one a subscription asks for: its kind, and each argument."""
    return event.name == name and all(
        event.data.get(key) == value for key, value in arguments.items()
    )


def _occurrence(event: _Event, cursor: str | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "eventId": event.event_id,
        "name": event.name,
        "timestamp": event.timestamp,
        "data": event.data,
    }
    if cursor is not None:
        body["cursor"] = cursor
    return body


async def _post_over_http(url: str, headers: Mapping[str, str], body: bytes) -> int:
    async with httpx2.AsyncClient(timeout=5.0, follow_redirects=False) as http:
        answer = await http.post(url, headers=dict(headers), content=body)
    return answer.status_code


@dataclass(frozen=True, slots=True)
class Running:
    """A server running on a local port: its address, and the server itself."""

    url: str
    server: EventsServer


@contextlib.asynccontextmanager
async def running(server: EventsServer, port: int = 0) -> AsyncGenerator[Running]:
    """Run the server on a port of this machine (any free one by default) until the end."""
    app = server.app()

    async def emit(request: Request) -> Response:
        """`POST /emit {"name": ..., "data": {...}}`: send an event, for a live trial."""
        body = await request.json()
        event_id = await server.emit(str(body["name"]), dict(body.get("data") or {}))
        return JSONResponse({"eventId": event_id})

    async def routed(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["path"] == "/emit":
            response = await emit(Request(scope, receive))
            await response(scope, receive, send)
            return
        if scope["type"] == "http" and server.pass_needed is not None:
            given = Request(scope).headers.get("authorization")
            if given != f"Bearer {server.pass_needed}":
                refused = Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
                await refused(scope, receive, send)
                return
        await app(scope, receive, send)

    config = uvicorn.Config(
        routed, host="127.0.0.1", port=port, log_level="warning", lifespan="on", ws="none"
    )
    serving = uvicorn.Server(config)
    task = asyncio.create_task(serving.serve())
    try:
        while not serving.started:
            if task.done():
                await task
                raise RuntimeError("the events server did not start")
            await asyncio.sleep(0.01)
        bound = serving.servers[0].sockets[0].getsockname()[1]
        yield Running(f"http://127.0.0.1:{bound}/mcp", server)
    finally:
        server.drop()
        serving.should_exit = True
        await task
