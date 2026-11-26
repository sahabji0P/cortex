"""
MCP events: the kinds of event a gem's connections serve, and running a watch on one.

This is the connections' side of a watch (`McpEvents` in `gem_event.py`). A kind of
event is offered to a gem when its connection is on and open to the gem, and its
server listed the kind when the connection's copy was last made (see
`connection_ent.py`). A limit on one tool does not narrow events: it names a tool, and
an event is not one.

A watch is run here for as long as it lasts, by whichever delivery the server offers
for the kind, in this order:

- **stream** (`push`): one long request, held open, the server sending each event.
- **webhook**, only when the watch has a public address: the server posts each event
  there, signed with a secret made here for the watch, and the subscription is asked
  for again before it lapses. A post comes in through `posted`.
- **poll**: asked again and again, as often as the server asks, never more than once a
  second.

When the connection cannot be reached, or the stream drops, the watch comes back to the
server after a pause that grows, from the last cursor it had, so what happened in
between is sent then. An event is told once: one whose id was told before is not told
again. A server that will not take a webhook (it cannot reach the address, say) is
watched by poll instead, if it offers that. Each delivery the watch starts on is told to
the watch (`on_delivery`), so that it can be shown: poll, after such a webhook.

The connection is read again before each event is told and each time the server is
come back to: a watch whose connection is gone, turned off, closed to its gem, needing
a sign-in, or no longer listing the event stops, and says why. The server ending the
subscription stops it too.

An event's data reaches the model as compact JSON, cut to `EVENT_TEXT_LIMIT`.
"""

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import random
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast

from bson import ObjectId

from app.adapters.docdb_adp import DocDBAdapterClass
from app.adapters.mcp_adp import (
    McpAdapterClass,
    McpEnded,
    McpEventStream,
    McpFailure,
    McpMark,
    McpOccurrence,
    McpServer,
    McpVerification,
)
from app.config import settings
from app.entities.connection_ent import Connection, ConnectionEntityClass, ConnectionEvent
from app.entities.gem_event import (
    EVENT_TEXT_LIMIT,
    McpEventKind,
    McpWatch,
    OnMcpCursor,
    OnMcpDelivery,
    OnMcpEvent,
    WatchDelivery,
)

__all__ = ["McpEventsEntityClass", "WatchFailed"]

_log = logging.getLogger(__name__)

# The protocol's codes for a refusal that asking again would not change: no such
# method, bad arguments, no such event, not allowed, not offered, and an address the
# server could not reach or check.
_REFUSALS = frozenset({-32601, -32602, -32023, -32024, -32026, -32027})
# How many event ids and post ids of a watch are remembered, to know a repeat by.
_SEEN_MAX = 1_000
# How long a webhook subscription is asked for. It is asked for again when four fifths
# of what the server granted have gone.
_HOOK_TTL_MS = 3_600_000
_HOOK_RENEW_AT = 0.8
# How long stopping a webhook may take when its watch ends.
_UNSUBSCRIBE_SECONDS = 5.0
# How many failures in a row of a server that does not answer before a watch on it is
# failing. It is tried again all the same (see `watch_ent.py`), and shown as failing
# meanwhile. THE OWNER HAS NOT RULED ON THIS NUMBER.
_FAILING_AFTER = 5


class WatchFailed(Exception):
    """A watch cannot run, and will not by trying again. The words say why, plainly."""


@dataclass(slots=True)
class _Seen:
    """What a watch has had: the last cursor, the ids told, and whether it was ended."""

    cursor: str | None
    # The address of the server the watch began on. Another is another server.
    url: str | None = None
    ids: deque[str] = field(default_factory=lambda: deque[str](maxlen=_SEEN_MAX))
    posts: deque[str] = field(default_factory=lambda: deque[str](maxlen=_SEEN_MAX))
    # Set when the server ends the subscription, with why.
    ended: asyncio.Event = field(default_factory=asyncio.Event)
    reason: str = ""
    # Told the cursor when it moves with no event, so the watch keeps it.
    on_cursor: OnMcpCursor | None = None
    # Told the delivery the watch starts on, so the watch shows it.
    on_delivery: OnMcpDelivery | None = None

    async def using(self, delivery: WatchDelivery) -> None:
        """The watch starts on this delivery. Failing to show it is no harm, so it never
        stops the watch."""
        if self.on_delivery is not None:
            try:
                await self.on_delivery(delivery)
            except Exception as exc:
                _log.info("a watch's delivery could not be kept: %s", exc)

    async def moved(self, cursor: str | None) -> None:
        """The cursor has moved with no event. None leaves the last one: no place is lost.

        The watch is told it, so that it resumes from here, not from now. Failing to keep
        it costs nothing but a replay, so it never stops the watch.
        """
        if cursor is None or cursor == self.cursor:
            return
        self.cursor = cursor
        if self.on_cursor is not None:
            try:
                await self.on_cursor(cursor)
            except Exception as exc:
                _log.info("a watch's cursor could not be kept: %s", exc)


class _Again(Exception):
    """Something went wrong that may not go wrong next time: pause, and come back."""


class McpEventsEntityClass:
    """The kinds of event a gem may watch, and running a watch on one (`McpEvents`)."""

    def __init__(
        self,
        *,
        docdb: DocDBAdapterClass,
        mcp: McpAdapterClass,
        connections: ConnectionEntityClass,
        poll_floor: float = 1.0,
        poll_every: float = 30.0,
        pause: tuple[float, float] = (1.0, 300.0),
    ) -> None:
        self._docdb = docdb
        self._mcp = mcp
        self._connections = connections
        # Never polled, or a webhook renewed, more often than `poll_floor` seconds, and
        # polled every `poll_every` seconds when the server does not say. A pause after
        # a failure starts at the first of `pause` and doubles up to the second.
        self._poll_floor = poll_floor
        self._poll_every = poll_every
        self._pause = pause
        # What each watch running here, or posted to, has had, by its id.
        self._seen: dict[str, _Seen] = {}

    async def kinds_for(self, gem_id: ObjectId) -> list[McpEventKind]:
        """The kinds of event served by the connections open to the gem, in their order."""
        gem = str(gem_id)
        connections = await self._docdb.get_many(
            Connection, filters={"enabled": True}, sort=[("created_at", 1)]
        )
        return [
            McpEventKind(
                connection_id=str(connection.id),
                connection_key=connection.key,
                name=event.name,
                description=event.description,
                delivery=list(event.delivery),
                input_schema=cast("dict[str, object]", json.loads(event.input_schema)),
            )
            for connection in connections
            if _open(connection, gem)
            for event in connection.events
        ]

    async def watch(
        self,
        watch: McpWatch,
        on_event: OnMcpEvent,
        hook_url: str | None,
        on_cursor: OnMcpCursor | None = None,
        on_delivery: OnMcpDelivery | None = None,
    ) -> None:
        """Run a watch until it is cancelled. Raises `WatchFailed` when it cannot run,
        and when its server has not answered `_FAILING_AFTER` times in a row."""
        seen = self._seen[watch.watch_id] = _Seen(
            watch.cursor, on_cursor=on_cursor, on_delivery=on_delivery
        )
        try:
            connection, kind = await self._check(watch)
            seen.url = connection.url
            if "push" in kind.delivery:
                await self._stream(watch, seen, on_event)
            elif "webhook" in kind.delivery and hook_url is not None:
                await self._webhook(watch, seen, on_event, hook_url, poll="poll" in kind.delivery)
            elif "poll" in kind.delivery:
                await self._poll(watch, seen, on_event)
            else:
                raise WatchFailed(
                    f"The event {watch.event} can only be posted to an address, and there is none"
                )
        finally:
            self._seen.pop(watch.watch_id, None)

    async def posted(
        self, watch: McpWatch, headers: Mapping[str, str], body: bytes, on_event: OnMcpEvent
    ) -> bool:
        """A post came to a watch's webhook address: check it, and tell `on_event` of it.

        False, and nothing told, when it is not signed with the watch's secret, or its
        connection no longer lets the watch run. A post seen before is not told again.
        Raises what `on_event` raises: the server then posts it again.
        """
        running = self._seen.get(watch.watch_id)
        try:
            connection, _ = await self._check(watch, None if running is None else running.url)
        except WatchFailed:
            return False
        post = self._mcp.check_post(self._secret(watch, connection.url), headers, body)
        if post is None:
            return False
        seen = running or _Seen(watch.cursor, connection.url)
        if post.message_id in seen.posts:
            return True
        item = post.item
        if isinstance(item, McpOccurrence) and item.name == watch.event:
            try:
                await self._tell(watch, seen, on_event, item, item.cursor)
            except WatchFailed:
                return False
            except _Again as again:
                cause = again.__cause__
                raise (cause if isinstance(cause, Exception) else again) from None
        elif isinstance(item, McpMark):
            _log.warning("watch %s: the server says events were skipped", watch.watch_id)
            await seen.moved(item.cursor)
        elif isinstance(item, McpEnded):
            seen.reason = item.reason
            seen.ended.set()
        elif isinstance(item, McpVerification):  # pyright: ignore[reportUnnecessaryIsInstance]
            # Answering it means echoing it in the reply, which a bool cannot carry.
            _log.warning("watch %s: a check of the address cannot be answered", watch.watch_id)
        seen.posts.append(post.message_id)
        return True

    # ---- the deliveries

    async def _poll(self, watch: McpWatch, seen: _Seen, on_event: OnMcpEvent) -> None:
        await seen.using("poll")
        failures = 0
        while True:
            try:
                connection, _ = await self._check(watch, seen.url)
                asked_from = seen.cursor
                polled = await self._asking(
                    connection,
                    lambda server: self._mcp.poll_events(
                        server, watch.event, watch.arguments, seen.cursor
                    ),
                )
                if polled.truncated:
                    _log.warning("watch %s: the server says events were skipped", watch.watch_id)
                last = len(polled.events) - 1
                for number, occurrence in enumerate(polled.events):
                    # A poll gives one cursor for its batch: it goes with its last event.
                    # The ones before carry the cursor the batch was asked from, so a
                    # watch that stops among them asks for them again, and skips none.
                    cursor = polled.cursor if number == last else asked_from
                    await self._tell(watch, seen, on_event, occurrence, cursor)
                await seen.moved(polled.cursor)
            except _Again as again:
                failures += 1
                self._still_failing(failures, again)
                await asyncio.sleep(self._pause_for(failures))
                continue
            failures = 0
            if polled.has_more:
                continue
            asked = polled.next_poll_ms
            wait = self._poll_every if asked is None else asked / 1000
            await asyncio.sleep(max(self._poll_floor, wait))

    async def _stream(self, watch: McpWatch, seen: _Seen, on_event: OnMcpEvent) -> None:
        await seen.using("push")
        failures = 0
        while True:
            stream: McpEventStream | None = None
            try:
                connection, _ = await self._check(watch, seen.url)
                stream = await self._asking(
                    connection,
                    lambda server: self._mcp.open_stream(
                        server, watch.event, watch.arguments, seen.cursor
                    ),
                )
                if stream.start.truncated:
                    _log.warning("watch %s: the server says events were skipped", watch.watch_id)
                await seen.moved(stream.start.cursor)
                async for item in _dropping(stream):
                    # Only a stream that sends something counts as working: one the
                    # server confirms and closes at once is come back to ever more slowly.
                    failures = 0
                    if isinstance(item, McpOccurrence):
                        await self._tell(watch, seen, on_event, item, item.cursor)
                    else:
                        await self._check(watch, seen.url)
                        if item.truncated:
                            _log.warning(
                                "watch %s: the server says events were skipped", watch.watch_id
                            )
                        await seen.moved(item.cursor)
                raise WatchFailed(f"The server ended the watch: {stream.ended}")
            except _Again as again:
                failures += 1
                self._still_failing(failures, again)
                await asyncio.sleep(self._pause_for(failures))
            finally:
                if stream is not None:
                    await stream.aclose()

    async def _webhook(
        self, watch: McpWatch, seen: _Seen, on_event: OnMcpEvent, url: str, *, poll: bool
    ) -> None:
        await seen.using("webhook")
        secret = self._secret(watch, seen.url)
        subscribed = False
        failures = 0
        try:
            while True:
                connection, _ = await self._check(watch, seen.url)
                try:
                    granted = await self._asking(
                        connection,
                        lambda server: self._mcp.subscribe_events(
                            server,
                            watch.event,
                            watch.arguments,
                            url=url,
                            secret=secret,
                            cursor=seen.cursor,
                            ttl_ms=_HOOK_TTL_MS,
                        ),
                    )
                except _Again as again:
                    failures += 1
                    self._still_failing(failures, again)
                    await asyncio.sleep(self._pause_for(failures))
                    continue
                except WatchFailed:
                    if subscribed or not poll:
                        raise
                    _log.warning(
                        "watch %s: the server will not post to its address, so it is polled",
                        watch.watch_id,
                    )
                    await self._poll(watch, seen, on_event)
                    return
                subscribed = True
                failures = 0
                if granted.truncated:
                    _log.warning("watch %s: the server says events were skipped", watch.watch_id)
                await seen.moved(granted.cursor)
                # Asked for again before it lapses, and now and then when it never does: a
                # renewal is where the cursor moves on while nothing is posted.
                left = _HOOK_TTL_MS / 1000
                if granted.refresh_before is not None:
                    left = (granted.refresh_before - datetime.now(UTC)).total_seconds()
                wait = max(self._poll_floor, left * _HOOK_RENEW_AT)
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(wait):
                        await seen.ended.wait()
                if seen.ended.is_set():
                    subscribed = False  # the server has no subscription left to stop
                    raise WatchFailed(f"The server ended the watch: {seen.reason}")
        finally:
            if subscribed:
                await self._unsubscribe(watch, url)

    async def _unsubscribe(self, watch: McpWatch, url: str) -> None:
        """Ask the server to stop posting. Not asking is no harm: it lapses by itself."""
        connection = await self._docdb.get(
            Connection, filters={"id": ObjectId(watch.connection_id)}
        )
        if connection is None:
            return
        try:
            async with asyncio.timeout(_UNSUBSCRIBE_SECONDS):
                await self._connections.on_server(
                    connection,
                    lambda server: self._mcp.unsubscribe_events(
                        server, watch.event, watch.arguments, url
                    ),
                )
        except (McpFailure, TimeoutError) as exc:
            _log.info("watch %s: its webhook was not stopped: %s", watch.watch_id, exc)

    # ---- helpers

    async def _tell(
        self,
        watch: McpWatch,
        seen: _Seen,
        on_event: OnMcpEvent,
        occurrence: McpOccurrence,
        cursor: str | None,
    ) -> None:
        """Tell one event, unless it was told before. Its id is kept once it has been."""
        await self._check(watch, seen.url)
        if occurrence.event_id in seen.ids:
            return
        text = json.dumps(occurrence.data, ensure_ascii=False, separators=(",", ":"))
        try:
            await on_event(occurrence.name, _cut(text), occurrence.event_id, occurrence.at, cursor)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Not kept: the event is asked for again from the last cursor.
            _log.warning("watch %s: an event could not be kept: %s", watch.watch_id, exc)
            raise _Again from exc
        seen.ids.append(occurrence.event_id)
        if cursor is not None:
            seen.cursor = cursor

    async def _check(
        self, watch: McpWatch, url: str | None = None
    ) -> tuple[Connection, ConnectionEvent]:
        """The watch's connection and kind of event, read now. Raises `WatchFailed`.

        `url` is the address the watch began on: a connection moved to another is
        another server, whose events are not the ones watched.
        """
        found = None
        if ObjectId.is_valid(watch.connection_id):
            found = await self._docdb.get(Connection, filters={"id": ObjectId(watch.connection_id)})
        if found is None:
            raise WatchFailed("Its connection has been removed")
        if not found.enabled:
            raise WatchFailed(f"The connection {found.name} has been turned off")
        if not _open(found, str(watch.gem_id)):
            raise WatchFailed(f"The connection {found.name} is no longer open to this gem")
        if url is not None and found.url != url:
            raise WatchFailed(f"The connection {found.name} now points at another server")
        kind = next((event for event in found.events if event.name == watch.event), None)
        if kind is None:
            raise WatchFailed(f"The connection {found.name} does not serve {watch.event}")
        if found.signs_in and not found.signed_in():
            raise WatchFailed(f"The user needs to sign in to {found.name} again")
        return found, kind

    async def _asking[T](
        self, connection: Connection, ask: Callable[[McpServer], Awaitable[T]]
    ) -> T:
        """One request of the server, with its pass kept good. A refusal is `WatchFailed`."""
        try:
            return await self._connections.on_server(connection, ask)
        except McpFailure as exc:
            if exc.sign_in_needed:
                raise WatchFailed(f"The user needs to sign in to {connection.name} again") from exc
            if exc.code in _REFUSALS:
                raise WatchFailed(f"{connection.name} refused the watch: {exc.reason}") from exc
            _log.info("a watch on %s will try again: %s", connection.name, exc.reason)
            raise _Again from exc

    def _still_failing(self, failures: int, again: _Again) -> None:
        """Raise `WatchFailed` once a server has not answered too many times in a row.

        The watch is then shown as failing, with why, and is tried again from its cursor.
        """
        if failures >= _FAILING_AFTER:
            cause = again.__cause__
            reason = cause.reason if isinstance(cause, McpFailure) else str(cause or again)
            raise WatchFailed(f"Its server does not answer: {reason}") from again

    def _pause_for(self, failures: int) -> float:
        """How long to wait after this many failures in a row: it doubles, with a little
        chance in it, so watches that failed together do not all come back together."""
        first, most = self._pause
        return min(most, first * 2 ** (failures - 1)) * random.uniform(0.5, 1.0)

    def _secret(self, watch: McpWatch, url: str | None) -> str:
        """The secret a watch's webhook posts are signed with: made from the backend's own
        secret key, the watch and its server's address, so it is the same after a
        restart, is kept nowhere, and a server the connection has moved away from can
        no longer sign a post."""
        made_from = f"mcp-events:{watch.watch_id}:{url}".encode()
        key = hmac.new(settings.secret_key.encode(), made_from, hashlib.sha256).digest()
        return self._mcp.webhook_secret(key)


async def _dropping(stream: McpEventStream) -> AsyncGenerator[McpOccurrence | McpMark]:
    """A stream's items. A drop is `_Again`: the watch comes back from its cursor."""
    try:
        async for item in stream:
            yield item
    except McpFailure as exc:
        _log.info("an event stream dropped: %s", exc.reason)
        raise _Again from exc


def _open(connection: Connection, gem: str) -> bool:
    """Whether a connection is on and open to the gem. No tool's limit is looked at."""
    return connection.enabled and (connection.gems == "all" or gem in connection.gems)


def _cut(text: str) -> str:
    if len(text) <= EVENT_TEXT_LIMIT:
        return text
    return (
        f"{text[:EVENT_TEXT_LIMIT]}\n[The event was {len(text)} characters long. "
        f"Only the first {EVENT_TEXT_LIMIT} are shown.]"
    )
