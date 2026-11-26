"""
Watches: what a gem watches for, and the running of each one.

A gem can watch things: something that happens wakes it, and it does what its
instructions say. The gem makes a watch itself, with a tool call, and every watch
belongs to that one gem. The app shows a gem's watches, and the user can remove one.
They are kept in the `Watches` collection.

A watch has one of three sources (see `gem_event.py`):

- `mcp`: an event a connection's server sends (MCP events). It runs in a task of its
  own, through `McpEvents.watch`, for as long as the watch does. The cursor after each
  event is kept, so after a restart it goes on from there. It has a webhook address too,
  for a server that delivers by posting to one.
- `webhook`: posts to an address of ours, `{PUBLIC_URL}/v1/hooks/{token}`, such as a
  GitHub repo's own webhook. The token is random and cannot be guessed, and it is the
  secret the posts are signed with too. Nothing runs for it: a post comes to the hooks
  route (`hooked`). A post signed the way GitHub signs one must be signed with it. A
  GitHub `ping` wakes nobody.
- `timer`: every so many minutes, at least `TIMER_MIN_MINUTES`, in a task of its own.
  It is first due one interval after it is made. One that came due while the backend
  was down goes off once as soon as it is up, and then keeps its rhythm.

Whatever its source, an event is handed to the `EventSink`, which makes it part of the
gem's conversation.

A fuse keeps a gem from waking itself in a loop (it comments on a pull request, the
webhook fires, it wakes and comments again): a watch that brings more than
`_FUSE_EVENTS` events in an hour is paused, and brings no more.

A watch says how its events reach us now (`delivery`): a plain webhook by `webhook` from
when it is made, a watch on MCP events by whichever its connection started on last
(`McpEvents.watch` tells it), and a timer by none.

A watch is `active`, `failing` (it cannot run now, and is tried again: `reason` says
why) or `paused` (it brings no more events until it is removed and made again: `reason`
says why). A watch that fails is tried again after a while, twice as long each time it
fails again, up to ten minutes. One that runs again for a minute, or brings an event,
is active again. Whoever listens (the app's sockets) is told when a watch
is added, removed, or changes state.

The gem has four tools for its watches (`tools_for`): `watch` makes one, `unwatch`
removes one, `watches` lists its own, and `watchable` lists the MCP events it may watch:
those of the connections open to it.

A watch added or removed is written in the gem's log, as `watch.added` and
`watch.removed`. The gem does not remember either: they are things that happened.

The database is the truth: every change reads the watch again, and writes only the
fields it owns. What is kept in memory is only what runs. Every watch that is not
paused runs from `start` until `close`.
"""

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import secrets
from collections import deque
from collections.abc import Awaitable, Callable, Collection, Coroutine, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Literal, cast

from bson import ObjectId
from bson.errors import InvalidId
from pydantic import BaseModel, ConfigDict, Field
from pydantic_mongo import PydanticObjectId

from app.adapters.docdb_adp import DocDBAdapterClass
from app.config import settings
from app.core.errors import AppError, InvalidError, NotFoundError
from app.core.times import UtcDatetime
from app.entities.gem_ent import GemEntityClass
from app.entities.gem_event import (
    EVENT_TEXT_LIMIT,
    EventSink,
    GemEvent,
    McpEventKind,
    McpEvents,
    McpWatch,
    OnMcpCursor,
    OnMcpDelivery,
    OnMcpEvent,
    WatchDelivery,
    WatchSource,
)
from app.entities.gem_log import LogEntityClass
from app.entities.model import ToolSpec
from app.entities.tools import Tool

__all__ = [
    "HOOKS_PATH",
    "TIMER_MIN_MINUTES",
    "HookOutcome",
    "Listener",
    "RemovedBy",
    "Watch",
    "WatchEntityClass",
    "WatchState",
]

# Where a watch's webhook is posted to, under `settings.public_url`, followed by its
# token. The route is defined in `app/routers/hooks`.
HOOKS_PATH = "/v1/hooks"
# How many random bytes a webhook's token is made of.
_TOKEN_BYTES = 32

# A timer goes off at most this often. THE OWNER HAS NOT RULED ON THIS NUMBER.
TIMER_MIN_MINUTES = 5

# How long a failing watch waits before it is tried again: this the first time, twice
# as long each time after, and never longer than the most. THE OWNER HAS NOT RULED ON
# THE FIRST WAIT; the most is his.
_RETRY_FIRST_SECONDS = 10.0
_RETRY_MOST_SECONDS = 600.0
# An MCP watch that has run this long since it was last tried is taken to work again.
# THE OWNER HAS NOT RULED ON THIS NUMBER.
_WORKS_AFTER_SECONDS = 60.0

# The fuse: a watch that brings more than this many events within this long is paused.
# THE OWNER HAS NOT RULED ON THIS. What it has brought is counted in memory, so a
# restart starts the count again.
_FUSE_EVENTS = 60
_FUSE_WINDOW = timedelta(hours=1)
_FUSED = (
    f"It brought more than {_FUSE_EVENTS} events in an hour, so it was paused: the gem may "
    "be waking itself in a loop. Remove it, and it can be made again."
)

_log = logging.getLogger(__name__)

type WatchState = Literal["active", "failing", "paused"]
# Who removed a watch: the gem, with a tool call, or the user, in the app.
type RemovedBy = Literal["gem", "user"]
# What became of a post to a watch's webhook address: handed over, a GitHub `ping` that
# wakes nobody, refused for its signature, or not taken because the watch is paused.
type HookOutcome = Literal["taken", "ignored", "refused", "paused"]

# Told the gem's id and the watch's id of each watch added, removed, or whose state
# changes. It may raise: that is not passed on.
type Listener = Callable[[str, str], Coroutine[Any, Any, None]]


class Watch(BaseModel):
    """One thing a gem watches for."""

    __collection_name__: ClassVar[str] = "Watches"

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PydanticObjectId = Field(default_factory=ObjectId, alias="_id")
    gem_id: PydanticObjectId
    # What it is for, in the gem's own words. The gem is told it with every event.
    about: str
    source: WatchSource
    # For MCP events: the connection that serves them, its key as the gem named it, the
    # event, its arguments (as JSON text: a document's fields may not have every name an
    # argument can), and where the last event left off, or None to start from now.
    connection_id: str | None = None
    connection_key: str | None = None
    event: str | None = None
    arguments: str = "{}"
    cursor: str | None = None
    # For a webhook, and for MCP events posted to us: the last part of its address, and
    # the secret its posts are signed with. Never sent to the app.
    token: str | None = None
    # For a timer: how often it goes off, and when it is next due.
    every_minutes: int | None = None
    due_at: UtcDatetime | None = None
    # How its events reach us now. None for a timer, and for a watch on MCP events until
    # it first starts on one, as for one stored before watches kept it.
    delivery: WatchDelivery | None = None
    state: WatchState = "active"
    # Why it is failing or paused. None while it is active.
    reason: str | None = None
    created_at: UtcDatetime
    # When it last brought an event, and how many it has brought.
    last_event_at: UtcDatetime | None = None
    events: int = 0

    @property
    def hook_url(self) -> str | None:
        """The address its posts come to, or None if it has none now."""
        if self.token is None or not settings.public_url:
            return None
        return f"{settings.public_url.rstrip('/')}{HOOKS_PATH}/{self.token}"

    @property
    def mcp(self) -> McpWatch:
        """What it is as the connections run it. Only for a watch on MCP events."""
        if self.connection_id is None or self.event is None:
            raise ValueError("the watch is not on MCP events")
        return McpWatch(
            watch_id=str(self.id),
            gem_id=self.gem_id,
            connection_id=self.connection_id,
            event=self.event,
            arguments=json.loads(self.arguments),
            cursor=self.cursor,
        )


@dataclass(frozen=True, slots=True)
class _Running:
    """A watch's own task, and the gem it is of."""

    gem_id: ObjectId
    task: asyncio.Task[None]


_WATCH = ToolSpec(
    name="watch",
    description=(
        "Watch for something, so that it wakes you when it happens: you are told the event, "
        "and do what your instructions say. A watch is one of three: an event a connection "
        "sends (`watchable` lists the ones you can watch), a webhook (an address of ours "
        "that something outside, such as a GitHub repo, posts to: you are given its address "
        "and secret, to register it there yourself), or a timer. It runs until it is removed "
        "with `unwatch`."
    ),
    # Not strict: what a watch needs differs by its source, and an event's arguments are
    # whatever that event takes. NOT YET TRIED AGAINST EITHER PROVIDER.
    parameters={
        "type": "object",
        "properties": {
            "about": {
                "type": "string",
                "description": (
                    "What the watch is for, and what to do when it wakes you, in a sentence. "
                    "You are told it with every event."
                ),
            },
            "source": {
                "type": "string",
                "enum": ["event", "webhook", "timer"],
                "description": (
                    "event: an event a connection sends. webhook: posts to an address of "
                    "ours. timer: every so many minutes."
                ),
            },
            "connection": {
                "type": "string",
                "description": "For an event: its connection, as `watchable` names it.",
            },
            "event": {
                "type": "string",
                "description": "For an event: its name, as `watchable` names it.",
            },
            "arguments": {
                "type": "object",
                "description": "For an event: its arguments, as `watchable` says it takes.",
            },
            "every_minutes": {
                "type": "integer",
                "description": f"For a timer: how often, in minutes. At least {TIMER_MIN_MINUTES}.",
            },
        },
        "required": ["about", "source"],
    },
    strict=False,
)

_UNWATCH = ToolSpec(
    name="unwatch",
    description="Remove one of your watches: it wakes you no more.",
    parameters={
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "The watch's id, as `watches` lists it."}
        },
        "required": ["id"],
        "additionalProperties": False,
    },
)

_WATCHES = ToolSpec(
    name="watches",
    description="List your watches: what each is for, its id, and whether it works.",
    parameters={"type": "object", "properties": {}},
    strict=False,
)

_WATCHABLE = ToolSpec(
    name="watchable",
    description=(
        "List the events of your connections that you can watch, with the arguments each "
        "takes. A webhook or a timer can always be watched."
    ),
    parameters={"type": "object", "properties": {}},
    strict=False,
)


def _watch_id(value: str) -> PydanticObjectId:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError) as err:
        raise NotFoundError("Watch") from err


def _utc_now() -> datetime:
    return datetime.now(UTC)


class WatchEntityClass:
    """Make, read, run and remove gems' watches.

    `now` and `sleep` are the clock it keeps time by: tests hand in their own.
    """

    def __init__(
        self,
        *,
        docdb: DocDBAdapterClass,
        gems: GemEntityClass,
        log: LogEntityClass,
        sink: EventSink,
        mcp_events: McpEvents,
        now: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._docdb = docdb
        self._log = log
        self._sink = sink
        self._mcp_events = mcp_events
        self._now = now
        self._sleep = sleep
        # Each watch that runs, by its id, from `start` or when it is made.
        self._running: dict[str, _Running] = {}
        # One for each watch: its events, and its changes of state, are written under it.
        self._locks: dict[str, asyncio.Lock] = {}
        # When each watch brought its events of the last hour, for the fuse.
        self._recent: dict[str, deque[datetime]] = {}
        self._listeners: set[Listener] = set()
        # Telling listeners, and removing a deleted gem's watches, run on their own.
        self._chores: set[asyncio.Task[None]] = set()
        gems.when_deleted(self._forget)

    def listen(self, listener: Listener) -> None:
        """Tell `listener` of each watch added, removed or changing state, until `ignore`."""
        self._listeners.add(listener)

    def ignore(self, listener: Listener) -> None:
        self._listeners.discard(listener)

    async def start(self) -> None:
        """Run every watch that is not paused, as when the backend starts."""
        for watch in await self._docdb.get_many(
            Watch, filters={"state": {"$in": ["active", "failing"]}}
        ):
            self._run(watch)

    async def close(self) -> None:
        """Stop every watch that runs, as when the backend stops. The next `start` runs them."""
        for watch_id in list(self._running):
            await self._stop(watch_id)
        if self._chores:
            await asyncio.wait(list(self._chores))

    async def add_webhook(self, gem_id: ObjectId, about: str) -> Watch:
        """A watch on posts to an address of ours. Refused while there is no public address."""
        if not settings.public_url:
            raise InvalidError(
                "This backend has no public address (PUBLIC_URL), so nothing outside can post to it"
            )
        return await self._add(
            Watch(
                gem_id=gem_id,
                about=about,
                source="webhook",
                token=secrets.token_urlsafe(_TOKEN_BYTES),
                delivery="webhook",
                created_at=self._now(),
            )
        )

    async def add_timer(self, gem_id: ObjectId, about: str, every_minutes: int) -> Watch:
        """A timer, first due one interval from now."""
        if every_minutes < TIMER_MIN_MINUTES:
            raise InvalidError(f"A timer goes off at most every {TIMER_MIN_MINUTES} minutes")
        now = self._now()
        return await self._add(
            Watch(
                gem_id=gem_id,
                about=about,
                source="timer",
                every_minutes=every_minutes,
                due_at=now + timedelta(minutes=every_minutes),
                created_at=now,
            )
        )

    async def add_mcp(
        self, gem_id: ObjectId, about: str, kind: McpEventKind, arguments: dict[str, Any]
    ) -> Watch:
        """A watch on one kind of a connection's MCP events, from now on.

        Whether the gem may watch it is the caller's to check (`McpEvents.kinds_for`).
        """
        return await self._add(
            Watch(
                gem_id=gem_id,
                about=about,
                source="mcp",
                connection_id=kind.connection_id,
                connection_key=kind.connection_key,
                event=kind.name,
                arguments=json.dumps(arguments, ensure_ascii=False),
                token=secrets.token_urlsafe(_TOKEN_BYTES),
                created_at=self._now(),
            )
        )

    async def list_for(self, gem_id: ObjectId) -> list[Watch]:
        """The gem's watches, the newest first."""
        return await self._docdb.get_many(
            Watch, filters={"gem_id": gem_id}, sort=[("created_at", -1), ("_id", -1)]
        )

    async def get(self, gem_id: ObjectId, watch_id: str) -> Watch:
        """One of the gem's watches. Raises `NotFoundError` for one that is not the gem's."""
        found = await self._docdb.get(Watch, filters={"id": _watch_id(watch_id)})
        if found is None or found.gem_id != gem_id:
            raise NotFoundError("Watch")
        return found

    async def remove(self, gem_id: ObjectId, watch_id: str, *, by: RemovedBy) -> None:
        """Remove one of the gem's watches: it stops at once."""
        watch = await self.get(gem_id, watch_id)
        if await self._docdb.delete(Watch, {"id": watch.id}) == 0:
            raise NotFoundError("Watch")
        await self._stop(str(watch.id))
        self._locks.pop(str(watch.id), None)
        self._recent.pop(str(watch.id), None)
        await self._note(gem_id, "watch.removed", {"watch": str(watch.id), "by": by})
        self._tell(watch)

    async def end_for(self, gem_id: ObjectId) -> None:
        """Remove every watch of the gem, as when it is reset: they all stop at once."""
        for watch in await self.list_for(gem_id):
            with contextlib.suppress(NotFoundError):
                await self.remove(gem_id, str(watch.id), by="user")

    def tools_for(self, gem_id: ObjectId) -> list[Tool]:
        """The gem's tools for its watches: `watch`, `unwatch`, `watches` and `watchable`.

        They are the same for every gem, every time. A refusal raises, with words the gem
        can act on.
        """

        async def watch(arguments: str) -> str:
            return await self._refused_in_words(self._watch(gem_id, _arguments(arguments)))

        async def unwatch(arguments: str) -> str:
            watch_id = str(_arguments(arguments).get("id", ""))
            try:
                await self.remove(gem_id, watch_id, by="gem")
            except NotFoundError as err:
                raise ValueError(
                    f"You have no watch with the id {watch_id!r}: `watches` lists yours"
                ) from err
            return f"Removed the watch {watch_id}: it wakes you no more."

        async def watches(arguments: str) -> str:
            return _listed(await self.list_for(gem_id))

        async def watchable(arguments: str) -> str:
            return _kinds(await self._mcp_events.kinds_for(gem_id))

        return [
            Tool(_WATCH, watch, shown=lambda arguments: "Setting up a watch"),
            Tool(_UNWATCH, unwatch, shown=lambda arguments: "Removing a watch"),
            Tool(_WATCHES, watches, shown=lambda arguments: "Looking at its watches"),
            Tool(_WATCHABLE, watchable, shown=lambda arguments: "Looking at what it can watch"),
        ]

    async def _refused_in_words(self, work: Coroutine[Any, Any, str]) -> str:
        """`work`'s result, or its refusal in the words the gem is to read."""
        try:
            return await work
        except AppError as err:
            raise ValueError(err.message) from err

    async def _watch(self, gem_id: ObjectId, given: Mapping[str, Any]) -> str:
        """Make the watch a `watch` call asks for, and say what it is."""
        about = given.get("about")
        if not isinstance(about, str) or not about.strip():
            raise ValueError("Say what the watch is for, in `about`")
        source = given.get("source")
        if source == "timer":
            every = given.get("every_minutes")
            if not isinstance(every, int) or isinstance(every, bool):
                raise ValueError("A timer needs `every_minutes`, a whole number of minutes")
            made = await self.add_timer(gem_id, about.strip(), every)
            return (
                f"Watching, under the id {made.id}: a timer every {every} minutes. It first "
                f"goes off at {_shown_time(made.due_at)}."
            )
        if source == "webhook":
            made = await self.add_webhook(gem_id, about.strip())
            return (
                f"Watching, under the id {made.id}. Register this webhook where its events come "
                f"from:\nAddress: {made.hook_url}\nSecret: {made.token}\nContent type: "
                "application/json\nEach post to it wakes you. The secret is for that "
                "registration only: do not show it to anyone."
            )
        if source == "event":
            connection, event = given.get("connection"), given.get("event")
            arguments = given.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise ValueError("An event's `arguments` are an object")
            kinds = await self._mcp_events.kinds_for(gem_id)
            kind = next(
                (k for k in kinds if (k.connection_key, k.name) == (connection, event)), None
            )
            if kind is None:
                raise ValueError(
                    f"No connection open to you sends the event {event!r} of {connection!r}: "
                    "`watchable` lists the ones you can watch"
                )
            made = await self.add_mcp(
                gem_id, about.strip(), kind, cast("dict[str, Any]", arguments)
            )
            return f"Watching {kind.connection_key}'s {kind.name} events, under the id {made.id}."
        raise ValueError('`source` is "event", "webhook" or "timer"')

    async def find_hook(self, token: str) -> Watch | None:
        """The watch whose webhook address ends in this token, or None."""
        if not token:
            return None
        return await self._docdb.get(Watch, filters={"token": token})

    async def hooked(self, watch: Watch, headers: Mapping[str, str], body: bytes) -> HookOutcome:
        """A post came to the watch's webhook address: check it, and hand its event over.

        A watch on MCP events has the connections check it and read it. A plain webhook's
        post must be signed with the watch's token if it says it is signed (GitHub's
        `X-Hub-Signature-256`). Its event is named by `X-GitHub-Event`, or "webhook", and
        its id is `X-GitHub-Delivery`. Raises if the sink cannot keep it.
        """
        if watch.state == "paused":
            return "paused"
        if watch.source == "mcp":
            taken = await self._mcp_events.posted(
                watch.mcp, headers, body, self._on_event(watch.id)
            )
            return "taken" if taken else "refused"
        named = {name.lower(): value for name, value in headers.items()}
        signature = named.get("x-hub-signature-256")
        if watch.token is None or (
            signature is not None and not _signed(watch.token, body, signature)
        ):
            return "refused"
        name = named.get("x-github-event") or "webhook"
        if name == "ping":
            return "ignored"
        text = body.decode("utf-8", errors="replace")
        event_id = named.get("x-github-delivery")
        if not await self._deliver(watch.id, name, text, event_id, self._now(), {}):
            return "paused"
        return "taken"

    async def _add(self, watch: Watch) -> Watch:
        await self._docdb.insert(watch)
        await self._note(
            watch.gem_id,
            "watch.added",
            {"watch": str(watch.id), "source": watch.source, "about": watch.about},
        )
        self._run(watch)
        self._tell(watch)
        return watch

    def _run(self, watch: Watch) -> None:
        """Start the watch's own task, if its source needs one."""
        if watch.source == "timer":
            work = self._tick(watch.id)
        elif watch.source == "mcp":
            work = self._follow(watch.id)
        else:
            return
        key = str(watch.id)
        task = asyncio.create_task(work, name=f"watch {key}")
        self._running[key] = _Running(watch.gem_id, task)
        task.add_done_callback(lambda ended: self._ended(key, ended))

    def _ended(self, watch_id: str, task: asyncio.Task[None]) -> None:
        if (running := self._running.get(watch_id)) is not None and running.task is task:
            del self._running[watch_id]
        if not task.cancelled() and (failure := task.exception()) is not None:
            _log.warning("watch %s stopped running", watch_id, exc_info=failure)

    async def _stop(self, watch_id: str) -> None:
        """Stop a watch's task, and wait until it has stopped, unless it is the one asking."""
        running = self._running.pop(watch_id, None)
        if running is None:
            return
        running.task.cancel()
        if running.task is not asyncio.current_task():
            await asyncio.wait([running.task])

    def _forget(self, gem_id: ObjectId) -> None:
        """A gem is deleted: its watches go with it, with nothing written in its log."""
        for watch_id, running in list(self._running.items()):
            if running.gem_id == gem_id:
                del self._running[watch_id]
                running.task.cancel()
        self._chore(self._delete_all(gem_id))

    async def _delete_all(self, gem_id: ObjectId) -> None:
        try:
            await self._docdb.delete_many(Watch, {"gem_id": gem_id})
        except Exception:
            _log.warning("the watches of deleted gem %s could not be removed", gem_id)

    async def _tick(self, watch_id: PydanticObjectId) -> None:
        """Run a timer until it is stopped: wait until it is due, go off, and keep its rhythm.

        The next time it is due is kept with each time it goes off, so a restart keeps
        its rhythm. If it came due more than once while it could not go off, it goes off
        once, and is next due at the first time of its rhythm still to come.
        """
        retry = _RETRY_FIRST_SECONDS
        while True:
            try:
                watch = await self._docdb.get(Watch, filters={"id": watch_id})
                if watch is None or watch.state == "paused":
                    return
                if watch.due_at is None or watch.every_minutes is None:
                    raise ValueError("the timer has no rhythm")
                now = self._now()
                if watch.due_at > now:
                    await self._sleep((watch.due_at - now).total_seconds())
                    continue
                every = timedelta(minutes=watch.every_minutes)
                due = watch.due_at + every
                while due <= now:
                    due += every
                # The same id each time it is tried, so the sink can tell one it kept.
                event_id = f"timer:{watch.due_at.isoformat()}"
                await self._deliver(watch_id, "timer", "", event_id, now, {"due_at": due})
                retry = _RETRY_FIRST_SECONDS
            except Exception as exc:
                await self._failing(watch_id, exc)
                await self._sleep(retry)
                retry = min(retry * 2, _RETRY_MOST_SECONDS)

    async def _follow(self, watch_id: PydanticObjectId) -> None:
        """Run a watch on MCP events until it is stopped, and again whenever it fails.

        The connections run it (`McpEvents.watch`). If that raises, or ends, the watch is
        failing, and is tried again from the last cursor kept. Once a try has run for a
        while, the watch is active again.
        """
        retry = _RETRY_FIRST_SECONDS
        while True:
            try:
                watch = await self._docdb.get(Watch, filters={"id": watch_id})
                if watch is None or watch.state == "paused":
                    return
                attempt = asyncio.create_task(
                    self._mcp_events.watch(
                        watch.mcp,
                        self._on_event(watch_id),
                        watch.hook_url,
                        self._on_cursor(watch_id),
                        self._on_delivery(watch_id),
                    )
                )
                try:
                    if await self._lasts(attempt):
                        retry = _RETRY_FIRST_SECONDS
                        await self._change_state(watch_id, "active", None)
                    await attempt
                finally:
                    if not attempt.done():
                        attempt.cancel()
                        await asyncio.wait([attempt])
                raise RuntimeError("the connection stopped sending its events")
            except Exception as exc:
                await self._failing(watch_id, exc)
                await self._sleep(retry)
                retry = min(retry * 2, _RETRY_MOST_SECONDS)

    async def _lasts(self, attempt: asyncio.Task[None]) -> bool:
        """Whether a try is still running after a while, and so is taken to work."""
        waiting = asyncio.ensure_future(self._sleep(_WORKS_AFTER_SECONDS))
        try:
            await asyncio.wait([attempt, waiting], return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiting.cancel()
        return not attempt.done()

    def _on_cursor(self, watch_id: PydanticObjectId) -> OnMcpCursor:
        """What is told an MCP watch's cursor when it moves with no event: it is kept, so
        a watch that has brought nothing yet resumes from there after a restart."""

        async def on_cursor(cursor: str) -> None:
            await self._docdb.update(Watch, {"id": watch_id}, {"cursor": cursor})

        return on_cursor

    def _on_delivery(self, watch_id: PydanticObjectId) -> OnMcpDelivery:
        """What is told how an MCP watch gets its events, each time it starts on a
        delivery: it is kept, and listeners are told when it changes."""

        async def on_delivery(delivery: WatchDelivery) -> None:
            watch = await self._docdb.get(Watch, filters={"id": watch_id})
            if watch is None or watch.delivery == delivery:
                return
            changed = watch.model_copy(update={"delivery": delivery})
            if await self._store(changed, {"delivery"}):
                self._tell(changed)

        return on_delivery

    def _on_event(self, watch_id: PydanticObjectId) -> OnMcpEvent:
        """What is told each MCP event of a watch: it is handed over, then its cursor kept."""

        async def on_event(
            name: str, data: str, event_id: str | None, at: datetime | None, cursor: str | None
        ) -> None:
            fields = {} if cursor is None else {"cursor": cursor}
            await self._deliver(watch_id, name, data, event_id, at or self._now(), fields)

        return on_event

    async def _deliver(
        self,
        watch_id: PydanticObjectId,
        name: str,
        data: str,
        event_id: str | None,
        at: datetime,
        fields: Mapping[str, Any],
    ) -> bool:
        """Hand one event of a watch to the sink, and keep `fields` with it once it is taken.

        Returns False if the watch is gone or paused, or the fuse pauses it now: then
        nothing is handed over. Raises if the sink cannot keep it, and then nothing is
        kept.
        """
        async with self._lock(watch_id):
            watch = await self._docdb.get(Watch, filters={"id": watch_id})
            if watch is None or watch.state == "paused":
                return False
            now = self._now()
            recent = self._recent.setdefault(str(watch_id), deque())
            while recent and recent[0] <= now - _FUSE_WINDOW:
                recent.popleft()
            if len(recent) >= _FUSE_EVENTS:
                await self._pause(watch, _FUSED)
                return False
            await self._sink.arrived(
                GemEvent(
                    gem_id=watch.gem_id,
                    watch_id=str(watch.id),
                    source=watch.source,
                    name=name,
                    about=watch.about,
                    data=_cut(data),
                    at=at,
                    event_id=event_id,
                )
            )
            recent.append(now)
            changed = watch.model_copy(
                update={
                    **fields,
                    "last_event_at": now,
                    "events": watch.events + 1,
                    "state": "active",
                    "reason": None,
                }
            )
            await self._store(changed, {*fields, "last_event_at", "events", "state", "reason"})
            if watch.state != changed.state:
                self._tell(changed)
            return True

    async def _pause(self, watch: Watch, reason: str) -> None:
        """Pause the watch, and stop its task.

        The task is stopped last, and not waited for: it may be the one asking, and is
        then stopped at the next thing it waits on.
        """
        paused = watch.model_copy(update={"state": "paused", "reason": reason})
        if await self._store(paused, {"state", "reason"}):
            self._tell(paused)
        if (running := self._running.pop(str(watch.id), None)) is not None:
            running.task.cancel()

    async def _failing(self, watch_id: PydanticObjectId, failure: Exception) -> None:
        """The watch cannot run now: it is failing, and says why. It never raises."""
        reason = str(failure) or type(failure).__name__
        await self._change_state(watch_id, "failing", reason)

    async def _change_state(
        self, watch_id: PydanticObjectId, state: WatchState, reason: str | None
    ) -> None:
        """Put the watch in this state, and tell of it if that is a change. It never raises.

        A paused watch stays paused.
        """
        try:
            async with self._lock(watch_id):
                watch = await self._docdb.get(Watch, filters={"id": watch_id})
                if watch is None or watch.state == "paused":
                    return
                if (watch.state, watch.reason) == (state, reason):
                    return
                changed = watch.model_copy(update={"state": state, "reason": reason})
                if await self._store(changed, {"state", "reason"}):
                    self._tell(changed)
        except Exception:
            _log.warning("watch %s could not be marked %s", watch_id, state, exc_info=True)

    def _lock(self, watch_id: ObjectId) -> asyncio.Lock:
        return self._locks.setdefault(str(watch_id), asyncio.Lock())

    async def _store(self, watch: Watch, fields: Collection[str]) -> bool:
        """Write these fields of a watch, and no others. False if it is gone."""
        document = watch.model_dump(by_alias=True, include=set(fields))
        return await self._docdb.update(Watch, {"id": watch.id}, document) > 0

    async def _note(self, gem_id: ObjectId, kind: str, data: dict[str, Any]) -> None:
        """Write down in the gem's log that something happened. It never raises."""
        try:
            await self._log.open(gem_id)
        except Exception:
            _log.warning("a %s of gem %s could not be written down", kind, gem_id, exc_info=True)
            return
        self._log.note(gem_id, kind, None, data)

    def _tell(self, watch: Watch) -> None:
        """Tell every listener that this watch has changed, without waiting."""
        for listener in list(self._listeners):
            self._chore(listener(str(watch.gem_id), str(watch.id)))

    def _chore(self, work: Coroutine[Any, Any, None]) -> None:
        chore = asyncio.create_task(work)
        self._chores.add(chore)
        chore.add_done_callback(_done)
        chore.add_done_callback(self._chores.discard)


def _arguments(text: str) -> dict[str, Any]:
    given = json.loads(text) if text.strip() else {}
    if not isinstance(given, dict):
        raise ValueError("the arguments are not an object")
    return cast("dict[str, Any]", given)


def _shown_time(at: datetime | None) -> str:
    return "an unknown time" if at is None else at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _listed(watches: list[Watch]) -> str:
    """The gem's watches, as the gem reads them."""
    if not watches:
        return "You watch nothing now."
    lines: list[str] = []
    for watch in watches:
        if watch.source == "timer":
            what = (
                f"a timer every {watch.every_minutes} minutes, next at {_shown_time(watch.due_at)}"
            )
        elif watch.source == "mcp":
            what = f"{watch.connection_key}'s {watch.event} events"
        else:
            what = "a webhook"
        state = watch.state if watch.reason is None else f"{watch.state}: {watch.reason}"
        last = (
            ""
            if watch.last_event_at is None
            else f", the last at {_shown_time(watch.last_event_at)}"
        )
        lines.append(
            f"- {watch.id}: {what}. For: {watch.about} State: {state}. "
            f"{watch.events} event{'' if watch.events == 1 else 's'} so far{last}."
        )
    return "\n".join(lines)


def _kinds(kinds: list[McpEventKind]) -> str:
    """The MCP events a gem may watch, as the gem reads them."""
    if not kinds:
        return (
            "No connection open to you sends events you can watch. A webhook or a timer can "
            "still be watched."
        )
    return "\n".join(
        f"- connection {kind.connection_key}, event {kind.name}: {kind.description} "
        f"Arguments: {json.dumps(kind.input_schema, ensure_ascii=False)}"
        for kind in kinds
    )


def _signed(secret: str, body: bytes, signature: str) -> bool:
    """Whether `signature` is the body's, as GitHub signs one: `sha256=` and its HMAC."""
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), signature.encode())


def _cut(text: str) -> str:
    """An event's text, cut to `EVENT_TEXT_LIMIT` with a line saying so if it is longer."""
    if len(text) <= EVENT_TEXT_LIMIT:
        return text
    note = f"\n[The event was {len(text)} characters long. Only the first part is shown.]"
    return f"{text[: EVENT_TEXT_LIMIT - len(note)]}{note}"


def _done(chore: asyncio.Task[None]) -> None:
    """A chore has ended: a listener that could not be told is not a fault."""
    if not chore.cancelled() and (failure := chore.exception()) is not None:
        _log.debug("a watch's chore failed: %s", type(failure).__name__)
