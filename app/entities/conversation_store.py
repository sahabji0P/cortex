"""
Conversation store entity: each gem's one conversation, kept in the `ChatItems` collection.

A gem has one conversation, whether the user calls it or writes to it. It is kept as the
model is sent it: one document per item of the history (a message, a line of ours, a
tool call, or a call's result), in order. It is only ever added to. That is what lets
the database be the truth: every reply in writing, and every call, starts from what is
stored. What is kept in memory is in `conversation_ent.py`.

The collection keeps the name it had when only written chat was stored in it.

A gem's conversation goes when the gem does (see `GemEntityClass.delete`).
"""

import contextlib
from collections.abc import Sequence
from typing import ClassVar, Literal

from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field
from pydantic_mongo import PydanticObjectId

from app.adapters.docdb_adp import DocDBAdapterClass
from app.core.times import UtcDatetime
from app.entities.loop import ResultMark
from app.entities.model import Item

__all__ = ["ConversationItem", "ConversationStoreClass", "Mode"]

# How the conversation was held when an item was added: spoken in a call, or written.
type Mode = Literal["call", "chat"]


class ConversationItem(BaseModel):
    """One item of a gem's conversation, as it is stored."""

    __collection_name__: ClassVar[str] = "ChatItems"

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PydanticObjectId = Field(default_factory=ObjectId, alias="_id")
    gem_id: PydanticObjectId
    # Its place in the gem's conversation: 0 for the first item, then one more each.
    place: int = Field(ge=0)
    # When it was added. The user's message carries when it arrived.
    at: UtcDatetime
    # The item as the model is sent it.
    item: Item
    # For a tool call, what its `chat.tool` or `call.tool.call` frame said, if anything.
    shown: str | None = None
    # For a call's result, what it stands for beyond its text: the placeholder of a call
    # left running, or the answer of a `cancel_tool_call` that stopped one. What is
    # stored is read by this, never by the result's wording.
    mark: ResultMark | None = None
    # Whether it was added in a call or in writing. Items stored before calls were kept
    # here have none, and were all written.
    mode: Mode = "chat"


class ConversationStoreClass:
    """Read a gem's conversation, add to it, and forget it."""

    def __init__(self, docdb: DocDBAdapterClass) -> None:
        self._docdb = docdb

    async def load(self, gem_id: ObjectId) -> list[ConversationItem]:
        """The whole conversation, oldest first."""
        return await self._docdb.get_many(
            ConversationItem, filters={"gem_id": gem_id}, sort=[("place", 1)]
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

    async def forget(self, gem_id: ObjectId) -> None:
        await self._docdb.delete_many(ConversationItem, {"gem_id": gem_id})
