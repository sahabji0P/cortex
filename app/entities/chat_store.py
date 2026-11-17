"""
Chat store entity: each gem's written conversation, kept in the `ChatItems` collection.

The conversation is kept as the model is sent it: one document per item of the history
(a message, a tool call, or a call's result), in order. It is only ever added to. That
is what lets the database be the truth: every reply is made from what is stored. All
that is kept in memory between messages is the searches still running (see `chat_ent.py`).

A gem's conversation goes when the gem does (see `GemEntityClass.delete`).
"""

import contextlib
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, ClassVar

from bson import ObjectId
from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from pydantic_mongo import PydanticObjectId

from app.adapters.docdb_adp import DocDBAdapterClass
from app.entities.model import Item

__all__ = ["ChatItem", "ChatStoreClass"]


def _in_utc(value: datetime) -> datetime:
    # The database hands times back without a zone. They were written in UTC.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class ChatItem(BaseModel):
    """One item of a gem's written conversation, as it is stored."""

    __collection_name__: ClassVar[str] = "ChatItems"

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PydanticObjectId = Field(default_factory=ObjectId, alias="_id")
    gem_id: PydanticObjectId
    # Its place in the gem's conversation: 0 for the first item, then one more each.
    place: int = Field(ge=0)
    # When it was added. The user's message carries when it arrived.
    at: Annotated[datetime, AfterValidator(_in_utc)]
    # The item as the model is sent it.
    item: Item
    # For a tool call, what its `chat.tool` frame said, if it said anything.
    shown: str | None = None


class ChatStoreClass:
    """Read a gem's written conversation, add to it, and forget it."""

    def __init__(self, docdb: DocDBAdapterClass) -> None:
        self._docdb = docdb

    async def load(self, gem_id: ObjectId) -> list[ChatItem]:
        """The whole conversation, oldest first."""
        return await self._docdb.get_many(ChatItem, filters={"gem_id": gem_id}, sort=[("place", 1)])

    async def add(self, items: Sequence[ChatItem]) -> None:
        """Add a message and its reply together: all of the items, or none of them.

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
                await self._docdb.delete_many(ChatItem, {"id": {"$in": [i.id for i in items]}})
            raise

    async def forget(self, gem_id: ObjectId) -> None:
        await self._docdb.delete_many(ChatItem, {"gem_id": gem_id})
