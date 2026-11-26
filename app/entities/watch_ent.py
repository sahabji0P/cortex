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

Whatever its source, an event is handed to the `EventSink`, which makes it part of the
gem's conversation.

A watch is `active`, `failing` (it cannot run now, and is tried again: `reason` says
why) or `paused` (it brings no more events until it is removed and made again: `reason`
says why). Whoever listens (the app's sockets) is told when a watch is added, removed,
or changes state.

A watch added or removed is written in the gem's log, as `watch.added` and
`watch.removed`. The gem does not remember either: they are things that happened.

The database is the truth: every change reads the watch again, and writes only the
fields it owns. What is kept in memory is only what runs.
"""

import asyncio
import contextlib
import logging
import secrets
from collections.abc import Callable, Collection, Coroutine
from datetime import UTC, datetime
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
from app.entities.gem_event import WatchSource
from app.entities.gem_log import LogEntityClass

__all__ = [
    "HOOKS_PATH",
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


def _watch_id(value: str) -> PydanticObjectId:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError) as err:
        raise NotFoundError("Watch") from err


class WatchEntityClass:
    """Make, read and remove gems' watches."""

    def __init__(
        self,
        *,
        docdb: DocDBAdapterClass,
        gems: GemEntityClass,
        log: LogEntityClass,
    ) -> None:
        self._docdb = docdb
        self._log = log
        self._listeners: set[Listener] = set()
        # Telling listeners, and removing a deleted gem's watches, run on their own.
        self._chores: set[asyncio.Task[None]] = set()
        gems.when_deleted(self._forget)

    def listen(self, listener: Listener) -> None:
        """Tell `listener` of each watch added, removed or changing state, until `ignore`."""
        self._listeners.add(listener)

    def ignore(self, listener: Listener) -> None:
        self._listeners.discard(listener)

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
                created_at=datetime.now(UTC),
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
        self._tell(watch)
        return watch

    def _forget(self, gem_id: ObjectId) -> None:
        """A gem is deleted: its watches go with it, with nothing written in its log."""
        self._chore(self._delete_all(gem_id))

    async def _delete_all(self, gem_id: ObjectId) -> None:
        try:
            await self._docdb.delete_many(Watch, {"gem_id": gem_id})
        except Exception:
            _log.warning("the watches of deleted gem %s could not be removed", gem_id)

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

    def _chore(self, work: Coroutine[Any, Any, Any]) -> None:
        chore = asyncio.create_task(work)
        self._chores.add(chore)
        chore.add_done_callback(_done)
        chore.add_done_callback(self._chores.discard)


def _done(chore: asyncio.Task[Any]) -> None:
    """A chore has ended: one that failed is not a fault of anyone waiting on it."""
    if not chore.cancelled() and (failure := chore.exception()) is not None:
        _log.debug("a watch's chore failed: %s", type(failure).__name__)
