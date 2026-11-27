"""What every test checks without asking: a request is sent what its gem remembers.

The gem's log holds nothing of what a request was sent. It is read back as every entry
the gem remembered before the request, since the gem was last reset, as the latest cold
start and the latest compaction before it have them sent (see `gem_log.py`,
`cold_start.py` and `compaction.py`), and for a compaction's own request, the message
that asks for the summary after them. That is only true while a conversation hands each
thing to the log before the request that carries it, so every request made in every
test is checked against what its gem remembers at that moment, sent so.
"""

from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from bson import ObjectId

from app.entities.cold_start import Numbered, Reason
from app.entities.compaction import Compacted, asked, view
from app.entities.conversation_store import COLD, COMPACTED, REMEMBERED, RESET, LogEntry
from app.entities.gem_log import LogEntityClass, Recording
from app.entities.history import Added, Mode
from app.entities.model import Item


@pytest.fixture(autouse=True)
def every_request_is_sent_what_its_gem_remembers(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # What each gem was handed to remember through each log, by its number, less what
    # was given up. It is in the database or on its way there.
    handed: dict[tuple[int, ObjectId], dict[int, Numbered]] = {}
    # Likewise the cold starts each gem's log was handed: their lines, by their numbers.
    colds: dict[tuple[int, ObjectId], dict[int, int]] = {}
    differed: list[tuple[list[Item], list[Item]]] = []
    remember, give_up, request = LogEntityClass.remember, LogEntityClass.give_up, Recording.request
    record_cold = LogEntityClass.record_cold

    def remembering(self: LogEntityClass, gem_id: ObjectId, added: Added, mode: Mode) -> None:
        remember(self, gem_id, added, mode)
        if gem_id in self._gems:
            seq = self._gems[gem_id].next - 1
            numbered = Numbered(seq, added.item, added.mark, added.delivers)
            handed.setdefault((id(self), gem_id), {})[seq] = numbered

    def recording_cold(self: LogEntityClass, gem_id: ObjectId, reason: Reason, before: int) -> None:
        record_cold(self, gem_id, reason, before)
        if gem_id in self._gems:
            colds.setdefault((id(self), gem_id), {})[self._gems[gem_id].next - 1] = before

    def giving_up(self: LogEntityClass, gem_id: ObjectId) -> list[LogEntry]:
        dropped = give_up(self, gem_id)
        for entry in dropped:
            handed.get((id(self), gem_id), {}).pop(entry.seq, None)
            colds.get((id(self), gem_id), {}).pop(entry.seq, None)
        return dropped

    def remembered_now(recording: Recording) -> list[Item] | None:
        """What the recording's gem remembers at this moment, in order and as it is sent.

        None if it cannot be told.
        """
        log, gem_id = recording._log, recording._gem_id
        if log is None or gem_id is None or gem_id not in log._gems:
            return None  # nothing is written down, or the gem is gone
        collections = getattr(getattr(log._store, "_docdb", None), "collections", None)
        if collections is None:
            return None  # not one of the tests' own stores
        documents = [d for d in collections.get("GemLog", []) if d["gem_id"] == gem_id]
        since = max((d["seq"] for d in documents if d["kind"] == RESET), default=-1)
        stored = {
            d["seq"]: Numbered(
                d["seq"], d["data"]["item"], d["data"].get("mark"), d["data"].get("delivers")
            )
            for d in documents
            if d["kind"] in REMEMBERED
        }
        everything = {**stored, **handed.get((id(log), gem_id), {})}
        lines = {d["seq"]: d["data"]["before"] for d in documents if d["kind"] == COLD}
        lines.update(colds.get((id(log), gem_id), {}))
        cold = [before for seq, before in sorted(lines.items()) if seq > since]
        remembered = [numbered for seq, numbered in sorted(everything.items()) if seq > since]
        # A compaction is written at once, so the database has every one there is.
        compactions = sorted(
            (d["seq"], Compacted(d["data"]["summary"], d["data"]["through"]))
            for d in documents
            if d["kind"] == COMPACTED and d["seq"] > since
        )
        compacted = compactions[-1][1] if compactions else None
        return view(remembered, cold[-1] if cold else None, compacted)

    def requesting(self: Recording, turn: int | None, items: Sequence[Item], think: bool) -> Any:
        remembered = remembered_now(self)
        if remembered is not None and list(items[-1:]) == [asked()]:
            # A compaction's own request: the message that asks for the summary comes last.
            remembered = [*remembered, asked()]
        if remembered is not None and list(items) != remembered:
            differed.append((list(items), remembered))
        return request(self, turn, items, think)

    monkeypatch.setattr(LogEntityClass, "remember", remembering)
    monkeypatch.setattr(LogEntityClass, "give_up", giving_up)
    monkeypatch.setattr(LogEntityClass, "record_cold", recording_cold)
    monkeypatch.setattr(Recording, "request", requesting)
    yield
    assert not differed, (
        "a request was sent something other than what its gem remembered: "
        f"sent {differed[0][0]!r}, remembered {differed[0][1]!r}"
    )
