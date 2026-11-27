"""
Gem log entity: everything of a gem, written down as it happens, in one list.

A gem's log is one list of entries, in one order (see `conversation_store.py`). Each
thing is written once, as the kind of entry it is, at the moment it is final. This is
where every entry gets its number, and the one place entries are written from.

Some kinds are what the gem remembers: what the user said, a line of ours, what the gem
said, a tool call and its result, and an event it was told
(`conversation_store.REMEMBERED`). Each holds one item as a model is sent it, and the
gem's conversation is those entries in order, since its last reset. A conversation
hands each one over the moment it joins its history (`Recording.remember`),
so the log's order is the order the model was sent things in.

The other kinds are what happened. No model is ever sent them. Each belongs to a turn
unless it says otherwise:

- `call.opened`, `call.closed`   a call began, or ended. No turn. Writing has neither.
- `gem.reset`        the user reset the gem. No turn. The gem knows nothing from
                     before it: its conversation starts again after it. Unlike the
                     rest, it cannot be lost: a reset is done only once it is stored
                     (`record_reset`).
- `event.came`       an event came in, for a watch of the gem's (see `gem_event.py`),
                     with all that is known of it. No turn. Like a reset, it cannot be
                     lost: it is written at once, and the source that brought it is told
                     only once it is stored (`record_event`). The gem is told it later,
                     by an `event.received` that `delivers` it.
- `watch.added`, `watch.removed`  the gem made a watch, or it was removed (`by` the
                     `gem` or the `user`). No turn. Written by `watch_ent.py`.
- `turn.opened`      a reply was asked for. `by` says by what: the `user`, the `gem`
                     speaking first, a `background_result` that is to be told, or an
                     `event` that came in. Or it is no reply but a summary of the
                     conversation, which nobody is shown: `compaction`. `mode` is
                     `call` or `chat`.
- `settings.changed` the model, the instructions or the tools differ from what was
                     last written down. Only the parts that changed, each in full.
                     Written when a conversation says what its requests are made
                     with, before its turn and anything it remembers, in no turn. A
                     change found later (the way to the provider) is written just
                     before the request it applies to, in that request's turn.
- `cache.cold`       the conversation starts cold (see `cold_start.py`): `reason` is
                     `settings`, `compaction` or `quiet`, and `before` the number of the
                     last request sent. From here on, long tool results before `before` are sent
                     expired. Written after `settings.changed`, in no turn. Like what
                     the gem remembers, it must be stored: what is sent depends on it.
- `request.sent`     a request went to the model. It was sent everything the gem
                     remembered by then: every remembered entry before this one since
                     the last reset, and nothing else, as the latest `cache.cold` and
                     `context.compacted` before it have them sent (the second puts its
                     summary, a line of ours, in place of what came before its line). A
                     compaction's request is sent one more message after them: the one
                     that asks for the summary. `count` is how many items that is.
- `request.done`     how the request ended: `done`, `failed`, or `cut` while the model
                     was still producing. Everything the model produced, in full: its
                     text and its tool calls. When the first of it came, the tokens
                     charged for and their cost.
- `tool.started`     a tool call began to run. Its arguments are in the `request.done`
                     of the request that made it.
- `tool.backgrounded`  its reply was over while it still ran: it carries on in the
                     background, and the gem was given a placeholder as its result.
- `tool.done`        it ended: `done`, `failed` or `cancelled`, and how many `seconds`
                     it ran. `background` says whether it ended in the background, and
                     only then is its `result` here: nothing remembers it yet. It
                     reaches the gem later, as a stub call and a result that name it
                     (`delivers`). The result of a call that ended within its reply is
                     in the `tool.result` that remembers it, and nowhere else.

Every entry of a tool call names the turn that made the call, whenever it is written,
and the call by its `call_id`. So a call that went on in the background reads, in the
order of time: started, backgrounded, done, and then delivered, or cancelled, or closed
as lost after a restart (a stub whose `source` is `lost-call`).

- `context.compacted`  the conversation was compacted (see `compaction.py`), in the
                     turn of the request that wrote the summary: `summary`, `through`
                     (the first entry still sent whole) and `by` (always the `user`).
                     From here on, what the gem remembers before `through` is sent as
                     the summary. Like a reset, it cannot be lost: the compaction is
                     done only once it is stored (`record_compacted`).
- `turn.closed`      the reply was settled into what the gem remembers: `finished`,
                     `cut` or `failed`. What is kept of it is not written here: it is
                     the turn's `gem.said` entries, and a cut reply with none of them
                     left nothing.

What the model produced and what the gem remembers of it are two things, and both are
written: `request.done` holds all of a reply's text, which is what was charged for, and
a `gem.said` holds what was delivered of it, which may be less.

Times: an entry's `at` is when it was written down, which is when it happened, with one
exception. A reply is settled when the next thing happens (in a call: when the user next
speaks, a background result is told, or the call ends), so `turn.closed` says when the reply
was settled, not when it stopped. When the model stopped is `request.done`. A reply
the user cuts into is settled at that instant, and its request is stopped after: so a
`turn.closed` can come before the `request.done` of the request it cut. Entries of one
turn are told apart by `turn`, not by where they fall.

Tokens: the count is read from a reply that finished. A request cut while the model was
still producing has none read.
Its usage and cost are None, which means unknown, not nothing.

Each gem's entries are numbered here, in one place: a reply being written is not the
only thing that adds to a log. A tool call that outlives its reply adds to it when
nobody holds the gem. A gem's numbers are read from the database the first time the gem
is used, and counted in memory from then on. This process is the only one that writes.

Writing: every entry is numbered and told to whoever listens at once, and written to
the database behind the reply's back, in order. Nothing waits for it, with one
exception: whoever holds a conversation waits, before it lets the gem go or sends a
reply's last frame, until what the gem remembers is stored (`stored`). The two kinds of
entry differ in what a failed write means:

- What happened is lost, and nothing fails for it. Writing it down never holds a reply
  up and never fails one.
- What the gem remembers is kept and tried again, in order, and `stored` raises while it
  is not stored. Nothing remembered is ever stored ahead of something remembered before
  it, so what is stored of a conversation is always a start of it, and a tool call is
  never stored without its result. A `cache.cold` is kept the same way (`MUST_STORE`).
"""

import asyncio
import contextlib
import functools
import json
import logging
import time
from collections.abc import Callable, Coroutine, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from bson import ObjectId

from app.entities.cold_start import Reason
from app.entities.conversation_store import (
    COLD,
    COMPACTED,
    EVENT_CAME,
    REMEMBERED,
    RESET,
    ConversationStoreClass,
    LogEntry,
    remembered,
    stamp,
)
from app.entities.history import Added, Mode
from app.entities.instructions import Layer
from app.entities.loop import ToolCall
from app.entities.model import MODELS, Item, ModelCall, ModelName, Provider, Usage
from app.entities.prices import PRICED, cost
from app.entities.tools import Tool

__all__ = ["Listener", "LogEntityClass", "Recording", "RequestRecord", "TurnRecord"]

logger = logging.getLogger(__name__)

# The kinds of entry that must be stored, and are kept and tried again until they are:
# what the gem remembers, and a cold start, which decides how it is sent.
MUST_STORE: tuple[str, ...] = (*REMEMBERED, COLD)

# Is told each entry as it is written down: the gem's id, and the entry as a client is
# shown it.
type Listener = Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]]

# What opened a turn.
type OpenedBy = Literal["user", "gem", "background_result", "event", "compaction"]


class _Gem:
    """What is kept in memory of one gem's log."""

    def __init__(self, seq: int, turn: int, request: int, settings: dict[str, Any]) -> None:
        self.next = seq  # the next entry's number
        self.turn = turn  # the last turn's number
        self.request = request  # the last request's number
        # The model, instructions and tools as they were last written down.
        self.settings = settings
        # Entries not written to the database yet, in order.
        self.pending: list[LogEntry] = []
        self.writing: asyncio.Task[None] | None = None
        # Remembered entries that a write failed to store, in order, and what stopped
        # it. They are tried again, ahead of anything remembered after them.
        self.unstored: list[LogEntry] = []
        self.fault: Exception | None = None
        # The number of the last entry handed out when what could not be stored was
        # given up (see `give_up`). Nothing remembered up to it is held to be tried
        # again, whatever becomes of a write that was on its way at that moment.
        self.given_up = -1
        self.dropped = False
        # The number of the latest reset written down in this process, or -1.
        self.reset = -1


class LogEntityClass:
    def __init__(self, store: ConversationStoreClass) -> None:
        self._store = store
        self._gems: dict[ObjectId, _Gem] = {}
        self._listeners: set[Listener] = set()
        # Telling listeners runs on its own, so a slow socket holds up nothing.
        self._telling: set[asyncio.Task[None]] = set()

    def listen(self, listener: Listener) -> None:
        """Tell `listener` each entry as it is written down, until `ignore`."""
        self._listeners.add(listener)

    def ignore(self, listener: Listener) -> None:
        self._listeners.discard(listener)

    async def open(self, gem_id: ObjectId) -> None:
        """Read where the gem's log stands, if this is the first use of it."""
        if gem_id in self._gems:
            return
        # Asked together: this is waited for before a call opens. A turn's or a
        # request's own entry may have been lost while others that name it were
        # stored, so its number is read from every entry that could name it.
        last, turn, changes, *requests = await asyncio.gather(
            self._store.last(gem_id),
            self._store.last_turn(gem_id),
            self._store.of_kind(gem_id, "settings.changed"),
            self._store.last(gem_id, "request.sent"),
            self._store.last(gem_id, "request.done"),
            self._store.last(gem_id, "tool.started"),
        )
        settings: dict[str, Any] = {}
        for changed in changes:
            settings.update(changed.data)
        # Another task may have read it, and counted on, while this one waited.
        self._gems.setdefault(
            gem_id,
            _Gem(
                seq=0 if last is None else last.seq + 1,
                turn=turn,
                request=max(
                    (
                        int(named.data.get("request") or 0)
                        for named in requests
                        if named is not None
                    ),
                    default=0,
                ),
                settings=settings,
            ),
        )

    def take(self, gem_id: ObjectId, count: int = 1) -> range:
        """The next `count` numbers of the gem's log, which must be open."""
        kept = self._gems[gem_id]
        first = kept.next
        kept.next = first + count
        return range(first, first + count)

    def drop(self, gem_id: ObjectId) -> None:
        """Keep nothing of a gem whose log is gone. What is not written yet never is."""
        kept = self._gems.pop(gem_id, None)
        if kept is not None:
            kept.dropped = True
            kept.pending = []
            kept.unstored = []

    def recording(self, gem_id: ObjectId, mode: Mode) -> "Recording":
        """What one conversation with the gem, in `mode`, writes down. The log must be open."""
        return Recording(self, gem_id, mode)

    def note(
        self, gem_id: ObjectId, kind: str, turn: int | None, data: dict[str, Any]
    ) -> int | None:
        """Write down one thing that happened, and return its number. It never raises, and
        never waits.

        Nothing is written of a gem whose log is not open (one that was deleted): its
        number is then None.
        """
        try:
            if gem_id not in self._gems:
                return None
            (seq,) = self.take(gem_id)
            self._add(
                LogEntry(
                    gem_id=gem_id, kind=kind, seq=seq, at=datetime.now(UTC), turn=turn, data=data
                )
            )
            return seq
        except Exception:
            logger.warning("a %s of gem %s could not be written down", kind, gem_id, exc_info=True)
            return None

    def remember(self, gem_id: ObjectId, added: Added, mode: Mode) -> None:
        """One item has joined the gem's history, in `mode`: it is remembered from here on.

        It is numbered now, so the log has it in the order the model is sent it, and
        written behind the caller's back (see `stored`). Nothing is remembered of a gem
        whose log is not open: one that was deleted, and its conversation with it.
        """
        if gem_id not in self._gems:
            return
        (seq,) = self.take(gem_id)
        self._add(remembered(gem_id, seq, added, mode))

    def record_cold(self, gem_id: ObjectId, reason: Reason, before: int) -> None:
        """Write down that the conversation starts cold, with the line it draws.

        It is kept like what the gem remembers, and in order with it: `stored` raises
        while it is not stored, and whoever holds the conversation fails for it. Nothing
        is written of a gem whose log is not open.
        """
        if gem_id not in self._gems:
            return
        (seq,) = self.take(gem_id)
        self._add(
            LogEntry(
                gem_id=gem_id,
                kind=COLD,
                seq=seq,
                at=datetime.now(UTC),
                data={"reason": reason, "before": before},
            )
        )

    async def record_reset(self, gem_id: ObjectId) -> int:
        """Write down that the gem is reset, once it is stored. Return its number.

        The log must be open. This is not something that happened, to be lost if it
        cannot be written: the gem's conversation starts after it. So it is written at
        once, and raises if it could not be stored.
        """
        (seq,) = self.take(gem_id)
        entry = LogEntry(gem_id=gem_id, kind=RESET, seq=seq, at=datetime.now(UTC))
        await self._store.write([entry])
        self._gems[gem_id].reset = max(self._gems[gem_id].reset, seq)
        self._tell(gem_id, entry.shown())
        return seq

    async def record_compacted(
        self,
        gem_id: ObjectId,
        turn: int | None,
        *,
        summary: str,
        through: int,
    ) -> int:
        """Write down that the conversation is compacted, once it is stored. Return its number.

        The log must be open. Like a reset, it decides what the model is sent from here
        on, so it is written at once, and raises if it could not be stored. A write
        that is under way is finished, even when whoever waits for it is stopped.
        """
        (seq,) = self.take(gem_id)
        entry = LogEntry(
            gem_id=gem_id,
            kind=COMPACTED,
            seq=seq,
            at=datetime.now(UTC),
            turn=turn,
            data={"summary": summary, "through": through, "by": "user"},
        )
        await asyncio.shield(self._store.write([entry]))
        self._tell(gem_id, entry.shown())
        return seq

    async def record_event(self, gem_id: ObjectId, data: dict[str, Any]) -> LogEntry:
        """Write down that an event came in for the gem, once it is stored. Return its entry.

        The log must be open. Like a reset, this is not to be lost: whoever brought the
        event is told it is kept only once it is. So it is written at once, on its own,
        and raises if it could not be stored. It is not held with what the gem
        remembers, so a reply that cannot be stored never takes it down with it.
        """
        (seq,) = self.take(gem_id)
        entry = LogEntry(gem_id=gem_id, kind=EVENT_CAME, seq=seq, at=datetime.now(UTC), data=data)
        await self._store.write([entry])
        self._tell(gem_id, entry.shown())
        return entry

    def reset_since(self, gem_id: ObjectId, seq: int) -> bool:
        """Whether the gem was reset after entry `seq`, in this process, or its log is gone."""
        kept = self._gems.get(gem_id)
        return kept is None or kept.reset > seq

    async def written(self, gem_id: ObjectId | None = None) -> None:
        """Wait until every entry so far has been written, or could not be: of one gem, or all."""
        for key, kept in list(self._gems.items()):
            if gem_id is not None and key != gem_id:
                continue
            while kept.writing is not None and not kept.writing.done():
                await asyncio.wait({kept.writing})

    async def stored(self, gem_id: ObjectId) -> None:
        """Wait until everything the gem remembers so far is in the database.

        Raises what stopped it, if something of it could not be stored. It is tried
        once more first, and stays to be tried again: see `give_up`.
        """
        kept = self._gems.get(gem_id)
        if kept is None:
            return
        await self.written(gem_id)
        if kept.unstored:
            self._start_writing(gem_id, kept)
            await self.written(gem_id)
        if kept.unstored:
            raise kept.fault or RuntimeError("the gem's conversation could not be stored")

    def give_up(self, gem_id: ObjectId) -> list[LogEntry]:
        """Drop what the gem remembers and is not stored. Return what was dropped.

        Whoever was holding the conversation has failed for it, or was stopped, and will
        not try again. What comes next starts from what is stored, so none of this may
        be stored later: not what was held back, not what still waits to be written,
        and not what a write on its way at this moment turns out to have failed to
        store. (If that write succeeds, what it carried is stored, and whoever reads the
        conversation next waits for it and reads it.)
        """
        kept = self._gems.get(gem_id)
        if kept is None:
            return []
        waiting = [entry for entry in kept.pending if entry.kind in MUST_STORE]
        dropped = [*kept.unstored, *waiting]
        kept.pending = [entry for entry in kept.pending if entry.kind not in MUST_STORE]
        kept.unstored, kept.fault = [], None
        kept.given_up = kept.next - 1
        return dropped

    def turn_number(self, gem_id: ObjectId) -> int | None:
        kept = self._gems.get(gem_id)
        if kept is None:
            return None
        kept.turn += 1
        return kept.turn

    def request_number(self, gem_id: ObjectId) -> int | None:
        kept = self._gems.get(gem_id)
        if kept is None:
            return None
        kept.request += 1
        return kept.request

    def settings_changed(self, gem_id: ObjectId, current: dict[str, Any]) -> dict[str, Any]:
        """The parts of `current` that differ from what was last written down, now kept."""
        kept = self._gems.get(gem_id)
        if kept is None:
            return {}
        changed = {
            part: value for part, value in current.items() if kept.settings.get(part) != value
        }
        kept.settings.update(changed)
        return changed

    def _add(self, entry: LogEntry) -> None:
        """Tell whoever listens of an entry, and have it written."""
        kept = self._gems[entry.gem_id]
        kept.pending.append(entry)
        self._tell(entry.gem_id, entry.shown())
        self._start_writing(entry.gem_id, kept)

    def _start_writing(self, gem_id: ObjectId, kept: _Gem) -> None:
        if kept.writing is None or kept.writing.done():
            kept.writing = asyncio.get_running_loop().create_task(self._write(gem_id, kept))

    async def _write(self, gem_id: ObjectId, kept: _Gem) -> None:
        # What could not be stored before is tried again each time this runs, once.
        again = True
        while kept.pending or (again and kept.unstored):
            again = False
            # In order, and what was held back first: nothing remembered is stored
            # ahead of something remembered before it.
            held, kept.unstored = kept.unstored, []
            batch, kept.pending = [*held, *kept.pending], []
            try:
                await self._store.write(batch)
                kept.fault = None
            except BaseException as exc:
                # What happened is lost. What the gem remembers is not: it is held, and
                # whoever waits for it to be stored is told (see `stored`). Unless it
                # was given up while this write was on its way.
                kept.unstored = [
                    entry
                    for entry in batch
                    if entry.kind in MUST_STORE and entry.seq > kept.given_up
                ]
                if any(entry.kind == "settings.changed" for entry in batch):
                    # A change that was not written down is not known to the log, so
                    # the next request says everything it is made with again.
                    kept.settings = {}
                if not isinstance(exc, Exception):
                    # This writer was stopped, not the write refused. What it held is
                    # kept for the next one, and nobody is told it is stored.
                    raise
                if kept.unstored:
                    kept.fault = exc
                logger.warning(
                    "%d entries of gem %s's log could not be written", len(batch), gem_id
                )
            if kept.dropped:
                # The gem was deleted while this was being written: it goes too.
                kept.unstored = []
                with contextlib.suppress(Exception):
                    await self._store.forget(gem_id)

    def _tell(self, gem_id: ObjectId, entry: dict[str, Any]) -> None:
        for listener in list(self._listeners):
            telling = asyncio.get_running_loop().create_task(listener(str(gem_id), entry))
            self._telling.add(telling)
            telling.add_done_callback(_told)
            telling.add_done_callback(self._telling.discard)


def _told(telling: asyncio.Task[None]) -> None:
    """A listener that has gone or failed is no reason to stop writing down."""
    if not telling.cancelled():
        telling.exception()


def _now() -> str:
    return stamp(datetime.now(UTC))


class Recording:
    """What one conversation writes down of a gem. Made with no log, it writes nothing."""

    def __init__(
        self, log: LogEntityClass | None = None, gem_id: ObjectId | None = None, mode: Mode = "chat"
    ) -> None:
        self._log = log
        self._gem_id = gem_id
        self.mode: Mode = mode
        self._model: ModelName | None = None
        # The provider of the model, once the conversation is described.
        self.provider: Provider | None = None
        # Whether the conversation started cold (`cold`).
        self.started_cold = False
        self._settings: Callable[[], dict[str, Any]] | None = None

    def describe(
        self,
        *,
        model: ModelName,
        via: Callable[[], str],
        layers: Sequence[Layer],
        tools: Sequence[Tool],
    ) -> bool:
        """Say what the conversation's requests are made with, and write down what changed.

        `via` is asked again at every request: the way to a provider can change while a
        call is open. The rest is fixed for the conversation. Returns whether anything
        differs from what was last written down: the conversation then starts cold.
        """
        self._model = model
        provider = self.provider = next(info.provider for info in MODELS if info.name == model)
        instructions = [{"name": layer.name, "text": layer.text} for layer in layers]
        told = [
            {
                "name": tool.spec.name,
                "description": tool.spec.description,
                # As text, laid out one way every time: a schema's own keys are not
                # ones a database takes, and it is compared and shown as text.
                "parameters": json.dumps(tool.spec.parameters, indent=2, sort_keys=True),
                "connection": tool.connection,
                "deferred": tool.spec.deferred,
            }
            for tool in tools
        ]

        def settings() -> dict[str, Any]:
            return {
                "model": {"name": model, "provider": provider, "via": via()},
                "instructions": instructions,
                "tools": told,
            }

        self._settings = settings
        # The instructions and the tools are the first thing a model is sent, so a change
        # of them is written down first too: before the turn, and before anything of the
        # conversation is remembered. It belongs to no turn, and holds from here on.
        return self._changes(None)

    def _changes(self, turn: int | None) -> bool:
        """Write down what the requests are made with now, if it differs from the last.

        Returns whether it did.
        """
        with contextlib.suppress(Exception):
            if self._log is not None and self._gem_id is not None and self._settings is not None:
                changed = self._log.settings_changed(self._gem_id, self._settings())
                if changed:
                    self.note("settings.changed", turn, changed)
                    return True
        return False

    def cold(self, reason: Reason, before: int) -> None:
        """The conversation starts cold: results before `before` are sent expired from here on."""
        self.started_cold = True
        if self._log is not None and self._gem_id is not None:
            self._log.record_cold(self._gem_id, reason, before)

    def call_opened(self) -> None:
        self.note("call.opened", None, {})

    def call_closed(self) -> None:
        self.note("call.closed", None, {})

    def remember(self, added: Added) -> None:
        """An item has joined the conversation's history: the gem remembers it from here on."""
        if self._log is not None and self._gem_id is not None:
            self._log.remember(self._gem_id, added, self.mode)

    def turn(self, by: OpenedBy) -> "TurnRecord":
        """A reply is asked for: open its turn."""
        number = None
        if self._log is not None and self._gem_id is not None:
            number = self._log.turn_number(self._gem_id)
        record = TurnRecord(self, number)
        self.note("turn.opened", number, {"by": by, "mode": self.mode})
        return record

    def note(self, kind: str, turn: int | None, data: dict[str, Any]) -> int | None:
        """Write down one thing that happened, and return its number, if it has one."""
        if self._log is not None and self._gem_id is not None:
            return self._log.note(self._gem_id, kind, turn, data)
        return None

    def request(self, turn: int | None, items: Sequence[Item], think: bool) -> "RequestRecord":
        """A request is about to be sent: write down what changed, and that it is sent.

        Most changes are written when the conversation is described. What is written
        here, with the request's turn, is what changed since: the way to the provider,
        or all of it again when the last change could not be written down.

        It is sent `items`, which are what the gem remembers by now: every one of them
        is in the log already, before this entry.
        """
        number = seq = None
        self._changes(turn)
        with contextlib.suppress(Exception):
            if self._log is not None and self._gem_id is not None:
                number = self._log.request_number(self._gem_id)
                seq = self.note(
                    "request.sent",
                    turn,
                    {"request": number, "attempt": 1, "think": think, "count": len(items)},
                )
        record = RequestRecord(self, turn, number, self._model)
        record.seq = seq
        return record


class TurnRecord:
    """What is written down of one turn: one reply, from when it is asked for.

    It is what the loop writes a reply's requests and tool calls down with (see
    `loop.Record`).
    """

    def __init__(self, recording: Recording, number: int | None) -> None:
        self._recording = recording
        self.number = number
        self._request: int | None = None
        self._error: str | None = None
        self._closed = False

    def sent(self, items: Sequence[Item], think: bool) -> "RequestRecord":
        request = self._recording.request(self.number, items, think)
        self._request = request.number
        return request

    def tool(self, call: ToolCall) -> None:
        """A tool call has started running: write that down, and its end when it comes."""
        with contextlib.suppress(Exception):
            self._recording.note(
                "tool.started",
                self.number,
                {
                    "request": self._request,
                    "call_id": call.call_id,
                    "name": call.name,
                    "connection": None if call.tool is None else call.tool.connection,
                },
            )
            if call.running is not None:
                began = time.monotonic()
                call.running.add_done_callback(functools.partial(self._tool_done, call, began))

    def backgrounded(self, call: ToolCall) -> None:
        """The reply is over and the call still runs: it carries on in the background."""
        with contextlib.suppress(Exception):
            self._recording.note("tool.backgrounded", self.number, {"call_id": call.call_id})

    def failed(self, error: BaseException) -> None:
        """The reply failed. Said when its turn is closed."""
        self._error = str(error) or type(error).__name__

    def closed(self, *, finished: bool, kept: str | None) -> None:
        """The reply is settled. `kept` is the text kept of a cut reply, None if it was not cut.

        It says how the turn closed, and is not written down itself: the entries that
        remember the reply hold it.
        """
        if self._closed:
            return
        self._closed = True
        if self._error is not None:
            outcome = "failed"
        elif finished and kept is None:
            outcome = "finished"
        else:
            outcome = "cut"
        self._recording.note("turn.closed", self.number, {"outcome": outcome, "error": self._error})

    def _tool_done(self, call: ToolCall, began: float, ran: asyncio.Task[str]) -> None:
        with contextlib.suppress(Exception):
            if ran.cancelled():
                outcome, result = "cancelled", None
            elif ran.exception() is not None:
                outcome, result = "failed", str(ran.exception())
            else:
                outcome, result = ("failed" if call.failed else "done"), ran.result()
            # Whether its reply was over before it was: it went on in the background.
            background = call.mark == "placeholder"
            data: dict[str, Any] = {
                "call_id": call.call_id,
                "outcome": outcome,
                "seconds": round(time.monotonic() - began, 3),
                "background": background,
            }
            if background and result is not None:
                # Nothing remembers it yet: it waits to be told, and until it is, this
                # is the one place it is written. A result that came within its reply
                # is in the entry that remembers it.
                data["result"] = result
            self._recording.note("tool.done", self.number, data)


class RequestRecord:
    """What is written down of one request to a model, from when it is sent."""

    def __init__(
        self, recording: Recording, turn: int | None, number: int | None, model: ModelName | None
    ) -> None:
        self._recording = recording
        self._turn = turn
        self.number = number
        # The number of its `request.sent` in the gem's log, once that is written down.
        self.seq: int | None = None
        self._model: ModelName | None = model
        self._first: str | None = None
        self._ended = False

    def first(self) -> None:
        """The first of the model's output has come."""
        if self._first is None:
            self._first = _now()

    def done(self, text: str, calls: Sequence[ModelCall], usage: Usage | None) -> None:
        """The model finished."""
        self._end("done", text, calls, usage, None)

    def stopped(self, text: str, error: BaseException) -> None:
        """The request did not finish: it failed, or it was cut while the model produced."""
        if isinstance(error, asyncio.CancelledError | GeneratorExit):
            self._end("cut", text, [], None, None)
        else:
            self._end("failed", text, [], None, str(error) or type(error).__name__)

    def _end(
        self,
        outcome: str,
        text: str,
        calls: Sequence[ModelCall],
        usage: Usage | None,
        error: str | None,
    ) -> None:
        if self._ended:
            return
        self._ended = True
        priced = None
        # A cost that cannot be worked out is unknown. The request's end is still written.
        with contextlib.suppress(Exception):
            priced = None if self._model is None else cost(self._model, usage)
        with contextlib.suppress(Exception):
            self._recording.note(
                "request.done",
                self._turn,
                {
                    "request": self.number,
                    "attempt": 1,
                    "outcome": outcome,
                    "text": text,
                    "calls": [
                        {"call_id": call.call_id, "name": call.name, "arguments": call.arguments}
                        for call in calls
                    ],
                    "first_token_at": self._first,
                    "usage": None
                    if usage is None
                    else {
                        "input": usage.input,
                        "cache_read": usage.cache_read,
                        "cache_write": usage.cache_write,
                        "output": usage.output,
                        "thinking": usage.thinking,
                    },
                    "cost_usd": None if priced is None else float(priced),
                    "priced": None if priced is None else PRICED,
                    "error": error,
                },
            )
