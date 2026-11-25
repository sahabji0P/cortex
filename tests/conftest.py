"""What every test checks without asking: a request is sent what its gem remembers.

The gem's log holds nothing of what a request was sent. It is read back as every entry
the gem remembered before the request, since the gem was last reset (see
`gem_log.py`). That is only true while a conversation hands each thing to the log before
the request that carries it, so every request made in every test is checked against what
its gem remembers at that moment.
"""

from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from bson import ObjectId

from app.entities.conversation_store import REMEMBERED, RESET, LogEntry
from app.entities.gem_log import LogEntityClass, Recording
from app.entities.history import Added, Mode
from app.entities.model import Item


@pytest.fixture(autouse=True)
def every_request_is_sent_what_its_gem_remembers(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # What each gem was handed to remember through each log, by its number, less what
    # was given up. It is in the database or on its way there.
    handed: dict[tuple[int, ObjectId], dict[int, Item]] = {}
    differed: list[tuple[list[Item], list[Item]]] = []
    remember, give_up, request = LogEntityClass.remember, LogEntityClass.give_up, Recording.request

    def remembering(self: LogEntityClass, gem_id: ObjectId, added: Added, mode: Mode) -> None:
        remember(self, gem_id, added, mode)
        if gem_id in self._gems:
            handed.setdefault((id(self), gem_id), {})[self._gems[gem_id].next - 1] = added.item

    def giving_up(self: LogEntityClass, gem_id: ObjectId) -> list[LogEntry]:
        dropped = give_up(self, gem_id)
        for entry in dropped:
            handed.get((id(self), gem_id), {}).pop(entry.seq, None)
        return dropped

    def remembered_now(recording: Recording) -> list[Item] | None:
        """What the recording's gem remembers at this moment, in order, if it can be told."""
        log, gem_id = recording._log, recording._gem_id
        if log is None or gem_id is None or gem_id not in log._gems:
            return None  # nothing is written down, or the gem is gone
        collections = getattr(getattr(log._store, "_docdb", None), "collections", None)
        if collections is None:
            return None  # not one of the tests' own stores
        documents = [d for d in collections.get("GemLog", []) if d["gem_id"] == gem_id]
        since = max((d["seq"] for d in documents if d["kind"] == RESET), default=-1)
        stored = {d["seq"]: d["data"]["item"] for d in documents if d["kind"] in REMEMBERED}
        everything = {**stored, **handed.get((id(log), gem_id), {})}
        return [item for seq, item in sorted(everything.items()) if seq > since]

    def requesting(self: Recording, turn: int | None, items: Sequence[Item], think: bool) -> Any:
        remembered = remembered_now(self)
        if remembered is not None and list(items) != remembered:
            differed.append((list(items), remembered))
        return request(self, turn, items, think)

    monkeypatch.setattr(LogEntityClass, "remember", remembering)
    monkeypatch.setattr(LogEntityClass, "give_up", giving_up)
    monkeypatch.setattr(Recording, "request", requesting)
    yield
    assert not differed, (
        "a request was sent something other than what its gem remembered: "
        f"sent {differed[0][0]!r}, remembered {differed[0][1]!r}"
    )
