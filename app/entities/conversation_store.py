"""
Conversation store entity: each gem's log, kept in the `GemLog` collection.

A gem has one log, whether the user calls it or writes to it. It holds two kinds of
entry, in one order (`seq`):

- What the gem remembers: an `item`, one thing the model is sent from then on, kept as
  the model is sent it (a message, a line of ours, a tool call, or a call's result).
  Items also have a `place`, their order among themselves. They are what makes the
  database the truth: every reply in writing, and every call, starts from the items
  stored.
- What happened: a turn opened, a request was sent, a tool finished (`LogEntry`). These
  are never sent to a model. They are there to be looked at (see `gem_log.py`).

The log is only ever added to. What is kept in memory is in `conversation_ent.py` and
`gem_log.py`.

A gem's log goes when the gem does (see `GemEntityClass.delete`).
"""

import contextlib
from collections.abc import Sequence
from datetime import datetime
from typing import Any, ClassVar, Literal

from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field
from pydantic_mongo import PydanticObjectId

from app.adapters.docdb_adp import DocDBAdapterClass
from app.core.times import UtcDatetime, in_utc
from app.entities.history import Mode, Source
from app.entities.loop import ResultMark
from app.entities.model import Item

__all__ = [
    "ConversationItem",
    "ConversationStoreClass",
    "LogEntry",
    "LogLine",
    "as_shown",
    "stamp",
]

_COLLECTION = "GemLog"


class ConversationItem(BaseModel):
    """One item of a gem's conversation, as it is stored: an entry of kind `item`."""

    __collection_name__: ClassVar[str] = _COLLECTION

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PydanticObjectId = Field(default_factory=ObjectId, alias="_id")
    gem_id: PydanticObjectId
    kind: Literal["item"] = "item"
    # Its number in the gem's log, among everything that happened. One written by hand,
    # as a test does, may have none.
    seq: int | None = None
    # Its place in the gem's conversation: 0 for the first item, then one more each.
    place: int = Field(ge=0)
    # When it was added. The user's message carries when it arrived.
    at: UtcDatetime
    # The item as the model is sent it.
    item: Item
    # Who wrote it. Items stored before this was kept have none.
    source: Source | None = None
    # For a tool call, what its `chat.tool` or `call.tool.call` frame said, if anything.
    shown: str | None = None
    # For a call's result, what it stands for beyond its text: the placeholder of a call
    # left running, or the answer of a `cancel_tool_call` that stopped one. What is
    # stored is read by this, never by the result's wording.
    mark: ResultMark | None = None
    # Whether it was added in a call or in writing.
    mode: Mode = "chat"


class LogEntry(BaseModel):
    """One thing that happened with a gem, as it is stored. Never sent to a model."""

    __collection_name__: ClassVar[str] = _COLLECTION

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PydanticObjectId = Field(default_factory=ObjectId, alias="_id")
    gem_id: PydanticObjectId
    # What happened, such as `turn.opened` or `request.done` (see `gem_log.py`).
    kind: str
    # Its number in the gem's log.
    seq: int
    at: UtcDatetime
    # The turn it belongs to, if it belongs to one.
    turn: int | None = None
    # What is known of it, by kind.
    data: dict[str, Any] = Field(default_factory=dict)


class LogLine(BaseModel):
    """Any entry of a gem's log, as it is read back to be shown: an item or a happening."""

    __collection_name__: ClassVar[str] = _COLLECTION

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True, extra="allow")

    id: PydanticObjectId = Field(alias="_id")
    gem_id: PydanticObjectId
    kind: str
    seq: int
    at: UtcDatetime
    turn: int | None = None

    def shown(self) -> dict[str, Any]:
        """The entry as a client is sent it: its own fields beside what every entry has."""
        extra = dict(self.model_extra or {})
        fields: dict[str, Any] = extra.pop("data", None) or {}
        fields.update({key: value for key, value in extra.items() if value is not None})
        return as_shown(self.seq, self.at, self.kind, self.turn, fields)


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

    async def load(self, gem_id: ObjectId) -> list[ConversationItem]:
        """The whole conversation, oldest first: the items, without what happened."""
        return await self._docdb.get_many(
            ConversationItem, filters={"gem_id": gem_id, "kind": "item"}, sort=[("place", 1)]
        )

    async def add(self, items: Sequence[ConversationItem]) -> None:
        """Add items that go together, such as a message and its reply: all, or none.

        They go in one request, but the database may still stop part-way, which would
        leave a tool call without its result. So what a failed write did put in is
        taken out again before the failure is raised.
        """
        if not items:
            return
        try:
            await self._docdb.insert_many(items)
        except BaseException:
            # If this cannot be done either (the database is out of reach), the part
            # that was written stays. That is the one way left to a half-stored reply.
            with contextlib.suppress(Exception):
                await self._docdb.delete_many(
                    ConversationItem, {"id": {"$in": [i.id for i in items]}}
                )
            raise

    async def note(self, entries: Sequence[LogEntry]) -> None:
        """Write down things that happened, in the order given."""
        if entries:
            await self._docdb.insert_many(entries)

    async def last(self, gem_id: ObjectId, kind: str | None = None) -> LogLine | None:
        """The gem's latest entry, of one kind if `kind` is given."""
        filters: dict[str, Any] = {"gem_id": gem_id}
        if kind is not None:
            filters["kind"] = kind
        else:
            # An item written by hand may have no number.
            filters["seq"] = {"$gte": 0}
        found = await self._docdb.get_many(LogLine, filters=filters, sort=[("seq", -1)], limit=1)
        return found[0] if found else None

    async def of_kind(self, gem_id: ObjectId, kind: str) -> list[LogLine]:
        """Every entry of one kind, oldest first."""
        return await self._docdb.get_many(
            LogLine, filters={"gem_id": gem_id, "kind": kind}, sort=[("seq", 1)]
        )

    async def page(self, gem_id: ObjectId, *, before: int | None, limit: int) -> list[LogLine]:
        """The `limit` entries just before number `before` (the latest if None), oldest first."""
        filters: dict[str, Any] = {"gem_id": gem_id, "seq": {"$gte": 0}}
        if before is not None:
            filters["seq"] = {"$gte": 0, "$lt": before}
        found = await self._docdb.get_many(
            LogLine, filters=filters, sort=[("seq", -1)], limit=limit
        )
        return found[::-1]

    async def one(self, gem_id: ObjectId, seq: int) -> LogLine | None:
        """The entry with that number, if there is one."""
        return await self._docdb.get(LogLine, filters={"gem_id": gem_id, "seq": seq})

    async def forget(self, gem_id: ObjectId) -> None:
        """Delete everything of a gem's log: what it remembers and what happened."""
        await self._docdb.delete_many(ConversationItem, {"gem_id": gem_id})
