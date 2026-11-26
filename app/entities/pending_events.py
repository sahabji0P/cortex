"""
Pending events: the events that came in for a gem and wait to be told, the same in a call
and in writing.

An event a watch of the gem's caught (see `gem_event.py`) is written down in the gem's
log as it comes in (`event.came`), and is told to the gem at its next turn, as an
`event.received` that delivers it (see `history.event_told`). Until then it waits here.
It never cuts a reply and is never an interruption: a turn that is running goes on, and
every event that came meanwhile goes into the next turn together.

A gem holds one `PendingEvents`, whether it is in a call or not (see
`conversation_ent.py`). What is the call's or chat's own is when the events are told: a
call tells them with the user's turn when it ends, or else once nothing is being said or
played; chat tells them with the message being answered, or else in a reply of their
own, once no reply is being written to the gem and no call holds it.

The log is the store: what waits here is the events since the gem's latest reset that no
`event.received` delivers. They are kept here, in memory, so the gem is woken when one
comes; the log is read again for them after a restart, and after events that were taken
to be told could not be stored (`lost`): they wait in the log, and are read back from it
the next time the gem's conversation is read.
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from app.entities.conversation_store import LogEntry, stamp
from app.entities.gem_event import GemEvent
from app.entities.history import Added, event_told

__all__ = ["PendingEvent", "PendingEvents", "came_fields", "pending", "told_events"]


@dataclass(frozen=True, slots=True)
class PendingEvent:
    """One event that came in, ready to be told to the gem."""

    # The number of its `event.came` entry in the gem's log.
    seq: int
    # The event as the gem is told it.
    told: Added

    @property
    def line(self) -> str:
        """What came in, in a short line for the user (see `history.event_line`)."""
        return self.told.shown or ""


def came_fields(event: GemEvent) -> dict[str, Any]:
    """What the `event.came` entry of an event holds: everything known of it.

    When it happened is `happened_at`: every entry's own `at` is when it was written.
    """
    return {
        "watch_id": event.watch_id,
        "source": event.source,
        "name": event.name,
        "about": event.about,
        "data": event.data,
        "happened_at": stamp(event.at),
        "event_id": event.event_id,
    }


def pending(entry: LogEntry) -> PendingEvent:
    """The event an `event.came` entry wrote down, ready to be told."""
    return PendingEvent(entry.seq, event_told(entry.data, entry.seq))


def told_events(events: Sequence[PendingEvent], turn: int | None = None) -> list[Added]:
    """Events as they join the history now, one item each, in the turn that tells them.

    They follow everything the model has been sent, and the late results told with
    them. What the user says next comes after them: a turn that ends while events wait
    is answered with the events in front of it, in one reply.
    """
    now = datetime.now(UTC)
    return [replace(event.told, at=now, turn=turn) for event in events]


class PendingEvents:
    """The events of one gem that came in and wait to be told, in the order they came.

    `ready` is called each time one comes in.
    """

    def __init__(self, ready: Callable[[], None]) -> None:
        self._ready = ready
        self._waiting: list[PendingEvent] = []
        # Whether events were taken to be told and could not be stored: they are read
        # again from the log, the next time the gem's conversation is read.
        self.stale = False

    @property
    def ready(self) -> bool:
        """Whether an event is waiting to be told."""
        return bool(self._waiting)

    @property
    def pending(self) -> bool:
        """Whether anything of them needs keeping: one waiting, or some to read again."""
        return bool(self._waiting) or self.stale

    def add(self, event: PendingEvent) -> None:
        """An event has come in, and is written down: it waits to be told."""
        if self._held(event.seq):
            return
        self._waiting.append(event)
        self._waiting.sort(key=lambda one: one.seq)
        self._ready()

    def take(self) -> list[PendingEvent]:
        """Every event waiting, in the order they came. They are no longer held here.

        The caller adds them to the history, all in one turn.
        """
        taken, self._waiting = self._waiting, []
        return taken

    def lost(self) -> None:
        """Events taken to be told could not be stored: read them again from the log.

        They are not put back at once, which would start another reply straight away,
        while the store may still be out of reach. They go with whatever reads the
        gem's conversation next: a message, another event, or a call.
        """
        self.stale = True

    def refresh(self, found: Iterable[PendingEvent]) -> None:
        """The events the log says are waiting, read again: each is held once."""
        for event in found:
            if not self._held(event.seq):
                self._waiting.append(event)
        self._waiting.sort(key=lambda one: one.seq)
        self.stale = False

    def drop_before(self, seq: int) -> None:
        """Drop every event that came before entry `seq`: a reset, which forgets them."""
        self._waiting = [event for event in self._waiting if event.seq > seq]

    def _held(self, seq: int) -> bool:
        return any(event.seq == seq for event in self._waiting)
