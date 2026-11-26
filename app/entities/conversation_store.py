"""
Conversation store entity: each gem's log, kept in the `GemLog` collection.

A gem has one log, whether the user calls it or writes to it. Every entry of it has the
same shape: its number in the log (`seq`), when it was written (`at`), what it is
(`kind`), the turn it belongs to if it belongs to one (`turn`), and what is known of it
(`data`). The log is only ever added to.

What an entry is, is said by its kind. Some kinds are what the gem remembers
(`REMEMBERED`): each holds one item exactly as a model is sent it, in `data.item`. The
gem's conversation is those entries since it was last reset, in the order of their
numbers, and nothing else: every reply in writing, and every call, starts from them.
That is what makes the database the truth.

- `user.said`     what the user said, typed or spoken
- `note.written`  a line of ours: a call started or ended, the gem is to greet
- `gem.said`      the gem's own text, as it was delivered
- `tool.called`   a tool call: the gem's own, or a stub of ours that carries a result
- `tool.result`   a call's result, as the gem was given it: the result itself, the
                  placeholder of a call that went on in the background, or what a stub
                  carries (`delivers` says for which call)
- `event.received`  an event a watch of the gem's caught, as the gem is told it: a user
                  message in an event tag, outside data and not the user's words
                  (`delivers` says which `event.came` it tells)

Every other kind is something that happened: a turn opened, a request was sent, a tool
finished. Those are never sent to a model. They are there to be looked at (see
`gem_log.py`, which says each kind).

Each thing is written once, as the kind of entry it is. What the user said is not also
written as something that happened: the entry that remembers it is the record of it.

A gem can be reset (`gem.reset`): it starts again, knowing nothing from before. Nothing
is taken out of the log: the conversation is what the gem remembers after the latest
such entry.

A conversation can start cold (`cache.cold`, see `cold_start.py`): from then on, long
tool results before its line are sent expired. What is stored stays as it was.

An event that comes in is written down at once (`event.came`), before the gem is told it,
and it waits until an `event.received` delivers it. The events waiting are those since
the latest reset that no `event.received` delivers yet (`pending_events`).

What is kept in memory is in `conversation_ent.py` and `gem_log.py`. A gem's log goes
when the gem does (see `GemEntityClass.delete`).
"""

import contextlib
from collections.abc import Sequence
from datetime import datetime
from typing import Any, ClassVar

from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field
from pydantic_mongo import PydanticObjectId

from app.adapters.docdb_adp import DocDBAdapterClass
from app.core.times import UtcDatetime, in_utc
from app.entities.cold_start import Cache
from app.entities.history import Added, Mode, Source
from app.entities.loop import ResultMark
from app.entities.model import Item

__all__ = [
    "COLD",
    "EVENT_CAME",
    "EVENT_RECEIVED",
    "REMEMBERED",
    "RESET",
    "ConversationStoreClass",
    "Kept",
    "LogEntry",
    "Remembered",
    "as_shown",
    "kind_of",
    "remembered",
    "stamp",
]

_COLLECTION = "GemLog"

# The kinds of entry that are what a gem remembers. An entry of any other kind is never
# sent to a model.
REMEMBERED: tuple[str, ...] = (
    "user.said",
    "note.written",
    "gem.said",
    "tool.called",
    "tool.result",
    "event.received",
)

# The kind of entry that resets a gem: its conversation starts again after it.
RESET = "gem.reset"

# The kind of entry that says a conversation started cold (see `cold_start.py`).
COLD = "cache.cold"

# The kind of entry that says an event came in, for a watch of the gem's.
EVENT_CAME = "event.came"

# The kind of entry that remembers an event, as the gem is told it.
EVENT_RECEIVED = "event.received"

_KIND_BY_ROLE = {"user": "user.said", "developer": "note.written", "assistant": "gem.said"}


def kind_of(item: Item, source: Source | None = None) -> str:
    """The kind of entry that remembers this item, written by `source`.

    An event is told to the gem as a user message, but it is not the user's: its kind
    is said by who wrote it, never by its role.
    """
    if source == "event":
        return EVENT_RECEIVED
    if "role" in item:
        return _KIND_BY_ROLE[item["role"]]
    return "tool.called" if item["type"] == "function_call" else "tool.result"


class Kept(BaseModel):
    """What is known of one thing a gem remembers: the `data` of such an entry."""

    # The item as the model is sent it.
    item: Item
    # Whether it was added in a call or in writing.
    mode: Mode = "chat"
    # Who wrote it (see `history.Source`).
    source: Source | None = None
    # For a tool call, what its `chat.tool` or `call.tool.call` frame said, if anything.
    shown: str | None = None
    # For a call's result, what it stands for beyond its text: the placeholder of a call
    # left running, or the answer of a `cancel_tool_call` that stopped one. What is
    # stored is read by this, never by the result's wording.
    mark: ResultMark | None = None
    # For a stub call of ours and its result: the id of the call whose result they
    # carry, which went on in the background or was lost. For an event: the number of
    # the `event.came` it tells.
    delivers: str | int | None = None


class LogEntry(BaseModel):
    """One entry of a gem's log, as it is stored: of any kind."""

    __collection_name__: ClassVar[str] = _COLLECTION

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PydanticObjectId = Field(default_factory=ObjectId, alias="_id")
    gem_id: PydanticObjectId
    # What it is, such as `user.said` or `request.done`.
    kind: str
    # Its number in the gem's log.
    seq: int
    at: UtcDatetime
    # The turn it belongs to, if it belongs to one.
    turn: int | None = None
    # What is known of it, by kind.
    data: dict[str, Any] = Field(default_factory=dict)

    def shown(self) -> dict[str, Any]:
        """The entry as a client is sent it: what every entry has, then its own fields."""
        return as_shown(self.seq, self.at, self.kind, self.turn, self.data)


class Remembered(BaseModel):
    """An entry of one of the kinds a gem remembers, as it is read back."""

    __collection_name__: ClassVar[str] = _COLLECTION

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PydanticObjectId = Field(alias="_id")
    gem_id: PydanticObjectId
    kind: str
    seq: int
    at: UtcDatetime
    turn: int | None = None
    data: Kept


def remembered(gem_id: ObjectId, seq: int, added: Added, mode: Mode) -> LogEntry:
    """The entry that remembers one item added to a gem's history, in `mode`."""
    kept = Kept(
        item=added.item,
        mode=mode,
        source=added.source,
        shown=added.shown,
        mark=added.mark,
        delivers=added.delivers,
    )
    return LogEntry(
        gem_id=gem_id,
        kind=kind_of(added.item, added.source),
        seq=seq,
        at=added.at,
        turn=added.turn,
        # Only what is known of it: a field with nothing to say is left out.
        data=kept.model_dump(exclude_none=True),
    )


def as_shown(
    seq: int, at: datetime, kind: str, turn: int | None, fields: dict[str, Any]
) -> dict[str, Any]:
    """An entry as a client is sent it: what every entry has, then its own fields."""
    return {"seq": seq, "at": stamp(at), "kind": kind, "turn": turn, **fields}


def stamp(at: datetime) -> str:
    """A time as the log shows it: in UTC, to the millisecond, which is what the database keeps.

    So an entry shows the same time when it is told as it happens and when it is read
    back.
    """
    return in_utc(at).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ConversationStoreClass:
    """Read a gem's log, add to it, and forget it."""

    def __init__(self, docdb: DocDBAdapterClass) -> None:
        self._docdb = docdb

    async def load(self, gem_id: ObjectId, *, before: int | None = None) -> list[Remembered]:
        """The gem's conversation, oldest first: what it remembers since it was last reset.

        With `before`, the conversation as it stood just before that entry.
        """
        return await self._docdb.get_many(
            Remembered,
            filters={
                "gem_id": gem_id,
                "kind": {"$in": list(REMEMBERED)},
                "seq": await self._since(gem_id, before),
            },
            sort=[("seq", 1)],
        )

    async def cache(self, gem_id: ObjectId, *, before: int | None = None) -> Cache:
        """Where the gem's cache stands, since it was last reset (see `cold_start.py`).

        The last request sent and the line of the latest cold start. With `before`, as
        it stood just before that entry.
        """
        seq = await self._since(gem_id, before)
        latest = [
            await self._docdb.get_many(
                LogEntry,
                filters={"gem_id": gem_id, "kind": kind, "seq": seq},
                sort=[("seq", -1)],
                limit=1,
            )
            for kind in ("request.sent", COLD)
        ]
        sent, started_cold = (found[0] if found else None for found in latest)
        return Cache(
            sent=None if sent is None else sent.seq,
            sent_at=None if sent is None else sent.at,
            before=None if started_cold is None else int(started_cold.data["before"]),
        )

    async def pending_events(self, gem_id: ObjectId) -> list[LogEntry]:
        """The events that came in since the gem was last reset and wait to be told, in order.

        Those no `event.received` delivers.
        """
        since = await self._since(gem_id, None)
        came, received = [
            await self._docdb.get_many(
                LogEntry, filters={"gem_id": gem_id, "kind": kind, "seq": since}, sort=[("seq", 1)]
            )
            for kind in (EVENT_CAME, EVENT_RECEIVED)
        ]
        told = {entry.data.get("delivers") for entry in received}
        return [entry for entry in came if entry.seq not in told]

    async def gems_with_events(self) -> list[ObjectId]:
        """Every gem an event ever came in for, in the order each first had one."""
        found = await self._docdb.get_many(LogEntry, filters={"kind": EVENT_CAME}, sort=[("at", 1)])
        return list(dict.fromkeys(ObjectId(entry.gem_id) for entry in found))

    async def _since(self, gem_id: ObjectId, before: int | None) -> dict[str, int]:
        """The numbers of the gem's entries since it was last reset, before `before` if given."""
        upto: dict[str, int] = {} if before is None else {"$lt": before}
        reset = await self._docdb.get_many(
            LogEntry,
            filters={"gem_id": gem_id, "kind": RESET, **({"seq": upto} if upto else {})},
            sort=[("seq", -1)],
            limit=1,
        )
        return {"$gte": reset[0].seq + 1 if reset else 0, **upto}

    async def write(self, entries: Sequence[LogEntry]) -> None:
        """Add entries to a log, in the order given: all, or none.

        They go in one request, but the database may still stop part-way, which could
        leave a tool call without its result. So what a failed write did put in is
        taken out again before the failure is raised.
        """
        if not entries:
            return
        try:
            await self._docdb.insert_many(entries)
        except BaseException:
            # If this cannot be done either (the database is out of reach), the part
            # that was written stays. That is the one way left to a half-stored reply.
            with contextlib.suppress(Exception):
                await self._docdb.delete_many(LogEntry, {"id": {"$in": [e.id for e in entries]}})
            raise

    async def last(self, gem_id: ObjectId, kind: str | None = None) -> LogEntry | None:
        """The gem's latest entry, of one kind if `kind` is given."""
        filters: dict[str, Any] = {"gem_id": gem_id}
        if kind is not None:
            filters["kind"] = kind
        found = await self._docdb.get_many(LogEntry, filters=filters, sort=[("seq", -1)], limit=1)
        return found[0] if found else None

    async def last_turn(self, gem_id: ObjectId) -> int:
        """The highest turn any entry of the gem's log names, or 0 if none names one."""
        found = await self._docdb.get_many(
            LogEntry,
            filters={"gem_id": gem_id, "turn": {"$gte": 0}},
            sort=[("turn", -1)],
            limit=1,
        )
        return found[0].turn or 0 if found else 0

    async def event_came(self, gem_id: ObjectId, watch_id: str, event_id: str) -> bool:
        """Whether the gem's log already holds that event of that watch, by its own id."""
        found = await self._docdb.get(
            LogEntry,
            filters={
                "gem_id": gem_id,
                "kind": EVENT_CAME,
                "data.watch_id": watch_id,
                "data.event_id": event_id,
            },
        )
        return found is not None

    async def of_kind(self, gem_id: ObjectId, kind: str) -> list[LogEntry]:
        """Every entry of one kind, oldest first."""
        return await self._docdb.get_many(
            LogEntry, filters={"gem_id": gem_id, "kind": kind}, sort=[("seq", 1)]
        )

    async def page(self, gem_id: ObjectId, *, before: int | None, limit: int) -> list[LogEntry]:
        """The `limit` entries just before number `before` (the latest if None), oldest first."""
        filters: dict[str, Any] = {"gem_id": gem_id}
        if before is not None:
            filters["seq"] = {"$lt": before}
        found = await self._docdb.get_many(
            LogEntry, filters=filters, sort=[("seq", -1)], limit=limit
        )
        return found[::-1]

    async def one(self, gem_id: ObjectId, seq: int) -> LogEntry | None:
        """The entry with that number, if there is one."""
        return await self._docdb.get(LogEntry, filters={"gem_id": gem_id, "seq": seq})

    async def forget(self, gem_id: ObjectId) -> None:
        """Delete everything of a gem's log: what it remembers and what happened."""
        await self._docdb.delete_many(LogEntry, {"gem_id": gem_id})
