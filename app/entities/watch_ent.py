"""
Watches: what a gem watches for, and the running of each one.

A gem can watch things: something that happens wakes it, and it does what its
instructions say. The gem makes a watch itself, with a tool call, and every watch
belongs to that one gem. The app shows a gem's watches, and the user can remove one.
They are kept in the `Watches` collection.

A watch has one of three sources (see `gem_event.py`):

- `webhook`: posts to an address of ours, `{PUBLIC_URL}/v1/hooks/{token}`, such as a
  GitHub repo's own webhook. The token is random and cannot be guessed, and it is the
  secret the posts are signed with too. Nothing runs for it: a post comes to the hooks
  route.
- `timer`: every so many minutes, at least `TIMER_MIN_MINUTES`, in a task of its own.
  It is first due one interval after it is made. One that came due while the backend
  was down goes off once as soon as it is up, and then keeps its rhythm.

Whatever its source, an event is handed to the `EventSink`, which makes it part of the
gem's conversation.

A watch is `active`, `failing` (it cannot run now, and is tried again: `reason` says
why) or `paused` (it brings no more events until it is removed and made again: `reason`
says why). A watch that fails is tried again after a while, twice as long each time it
fails again, up to ten minutes. Whoever listens (the app's sockets) is told when a watch
is added, removed, or changes state.

A watch added or removed is written in the gem's log, as `watch.added` and
`watch.removed`. The gem does not remember either: they are things that happened.

The database is the truth: every change reads the watch again, and writes only the
fields it owns. What is kept in memory is only what runs. Every watch that is not
paused runs from `start` until `close`.
"""

import asyncio
import contextlib
import logging
import secrets
from collections.abc import Awaitable, Callable, Collection, Coroutine, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Literal

from bson import ObjectId
from bson.errors import InvalidId
from pydantic import BaseModel, ConfigDict, Field
from pydantic_mongo import PydanticObjectId

from app.adapters.docdb_adp import DocDBAdapterClass
from app.config import settings
from app.core.errors import InvalidError, NotFoundError
from app.core.times import UtcDatetime
from app.entities.gem_ent import GemEntityClass
from app.entities.gem_event import EVENT_TEXT_LIMIT, EventSink, GemEvent, WatchSource
from app.entities.gem_log import LogEntityClass

__all__ = [
    "HOOKS_PATH",
    "TIMER_MIN_MINUTES",
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

_log = logging.getLogger(__name__)

type WatchState = Literal["active", "failing", "paused"]
# Who removed a watch: the gem, with a tool call, or the user, in the app.
type RemovedBy = Literal["gem", "user"]

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
    # For a webhook: the last part of its address, and the secret its posts are signed
    # with. Never sent to the app.
    token: str | None = None
    # For a timer: how often it goes off, and when it is next due.
    every_minutes: int | None = None
    due_at: UtcDatetime | None = None
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


@dataclass(frozen=True, slots=True)
class _Running:
    """A watch's own task, and the gem it is of."""

    gem_id: ObjectId
    task: asyncio.Task[None]


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
        now: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._docdb = docdb
        self._log = log
        self._sink = sink
        self._now = now
        self._sleep = sleep
        # Each watch that runs, by its id, from `start` or when it is made.
        self._running: dict[str, _Running] = {}
        # One for each watch: its events, and its changes of state, are written under it.
        self._locks: dict[str, asyncio.Lock] = {}
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
        await self._note(gem_id, "watch.removed", {"watch": str(watch.id), "by": by})
        self._tell(watch)

    async def end_for(self, gem_id: ObjectId) -> None:
        """Remove every watch of the gem, as when it is reset: they all stop at once."""
        for watch in await self.list_for(gem_id):
            with contextlib.suppress(NotFoundError):
                await self.remove(gem_id, str(watch.id), by="user")

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
        if watch.source != "timer":
            return
        key = str(watch.id)
        task = asyncio.create_task(self._tick(watch.id), name=f"watch {key}")
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

        Returns False if the watch is gone or paused: then nothing is handed over. Raises
        if the sink cannot keep it, and then nothing is kept.
        """
        async with self._lock(watch_id):
            watch = await self._docdb.get(Watch, filters={"id": watch_id})
            if watch is None or watch.state == "paused":
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
            changed = watch.model_copy(
                update={
                    **fields,
                    "last_event_at": self._now(),
                    "events": watch.events + 1,
                    "state": "active",
                    "reason": None,
                }
            )
            await self._store(changed, {*fields, "last_event_at", "events", "state", "reason"})
            if watch.state != changed.state:
                self._tell(changed)
            return True

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
