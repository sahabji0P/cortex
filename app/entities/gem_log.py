"""
Gem log entity: the order of each gem's log, kept in memory.

Every entry of a gem's log has a number (`seq`), one more than the entry before it,
whatever kind it is and whoever writes it (see `conversation_store.py`). The numbers
are handed out here, in one place for each gem: a reply being written is not the only
thing that adds to a log. A tool call that outlives its reply adds to it when nobody
holds the gem.

A gem's next number is read from the database the first time the gem is used, and
counted in memory from then on. This process is the only one that writes.
"""

from bson import ObjectId

from app.entities.conversation_store import ConversationStoreClass

__all__ = ["LogEntityClass"]


class LogEntityClass:
    def __init__(self, store: ConversationStoreClass) -> None:
        self._store = store
        # Each gem's next number, once it has been read.
        self._next: dict[ObjectId, int] = {}

    async def open(self, gem_id: ObjectId) -> None:
        """Read where the gem's log stands, if this is the first use of it."""
        if gem_id in self._next:
            return
        last = await self._store.last(gem_id)
        # Another task may have read it, and counted on, while this one waited.
        self._next.setdefault(gem_id, 0 if last is None else last.seq + 1)

    def take(self, gem_id: ObjectId, count: int = 1) -> range:
        """The next `count` numbers of the gem's log, which must be open."""
        first = self._next[gem_id]
        self._next[gem_id] = first + count
        return range(first, first + count)

    def drop(self, gem_id: ObjectId) -> None:
        """Keep nothing of a gem whose log is gone."""
        self._next.pop(gem_id, None)
