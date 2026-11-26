"""
Events entity: where the events a gem watches for come in, and become part of its one
conversation.

A gem can watch things (see `gem_event.py`). Whatever the source, an event that comes in
is handed here (`arrived`), and this is the `EventSink` every source hands its events
to. It is written down in the gem's log at once (`event.came`), and only once it is
stored is the source told it is kept. Then it waits for the gem (see
`pending_events.py`):

- It never cuts a reply and is never an interruption. It waits for the gem's next turn,
  and goes in ahead of what the user says.
- A gem that is quiet is woken by the event itself: in writing, a reply nobody asked
  for, written and stored even when no app is connected (see `chat_ent.py`); in a call,
  at the next moment nobody is speaking and nothing is playing (see `voice_ent.py`).
- Events that come while a turn runs all go into the next turn together: a burst is one
  turn, not one each.

The gem is told it as a user message in an event tag, which says it is outside data and
not the user's words (`history.event_told`), and that is remembered as an
`event.received` that delivers it.

The log is the store, not this process. An event written down and not yet told is still
waiting after a restart: `resume` wakes every gem that has one. A reset drops the events
waiting, and an event of a gem that is deleted goes with its log.
"""

import logging

from bson import ObjectId

from app.core.errors import NotFoundError
from app.entities.conversation_ent import ConversationEntityClass
from app.entities.conversation_store import ConversationStoreClass
from app.entities.gem_ent import GemEntityClass
from app.entities.gem_event import EventSink, GemEvent
from app.entities.pending_events import came_fields, pending

__all__ = ["EventsEntityClass"]

logger = logging.getLogger(__name__)


class EventsEntityClass(EventSink):
    def __init__(
        self,
        *,
        gems: GemEntityClass,
        conversations: ConversationEntityClass,
        store: ConversationStoreClass,
    ) -> None:
        self._gems = gems
        self._conversations = conversations
        self._store = store
        # The events being kept now, by gem, watch and their own id: one delivered again
        # while the first is still being kept is not kept twice.
        self._keeping: set[tuple[str, str, str]] = set()

    async def arrived(self, event: GemEvent) -> bool:
        """An event has come in. Returns once it is stored, before the gem answers it.

        Raises `NotFoundError` if the gem is not there, and what stopped it if it could
        not be stored: the source may then try again. Nothing is kept of an event that
        raised. The gem is woken by it once this returns, whatever it is doing: it is
        told the event at its next turn.

        An event with its own id (`event_id`) is kept once: delivered again, as a source
        does when it tries again after a failure or a restart, it is passed over, and
        False is returned. True means it was kept now.
        """
        key = str(event.gem_id)
        if not await self._gems.exists(key):
            raise NotFoundError("Gem")
        if event.event_id is None:
            await self._keep(event)
            return True
        same = (key, event.watch_id, event.event_id)
        if same in self._keeping:
            return False
        self._keeping.add(same)
        try:
            if await self._store.event_came(event.gem_id, event.watch_id, event.event_id):
                return False
            await self._keep(event)
            return True
        finally:
            self._keeping.discard(same)

    async def _keep(self, event: GemEvent) -> None:
        log = self._conversations.log
        await log.open(event.gem_id)
        came = await log.record_event(event.gem_id, came_fields(event))
        if log.reset_since(event.gem_id, came.seq):
            # The gem was reset while this was written, or deleted: it is kept in the log
            # before the reset, and the gem knows nothing of it.
            return
        self._conversations.gem(str(event.gem_id)).events.add(pending(came))

    async def resume(self) -> None:
        """Wake every gem that has events waiting to be told, as after a restart.

        Called once, when the app starts. A gem that is not there any more is passed by.
        A gem whose events cannot be read now is not held up for: they wait in the log,
        and are read with the gem's conversation the next time it is read.
        """
        for gem_id in await self._store.gems_with_events():
            try:
                await self._wake(gem_id)
            except Exception:
                logger.warning("the events of gem %s could not be read", gem_id, exc_info=True)
                self._conversations.gem(str(gem_id)).events.lost()

    async def _wake(self, gem_id: ObjectId) -> None:
        if not await self._gems.exists(str(gem_id)):
            return
        waiting = await self._conversations.pending_events(gem_id)
        if not waiting:
            return
        await self._conversations.log.open(gem_id)
        events = self._conversations.gem(str(gem_id)).events
        for event in waiting:
            events.add(event)
