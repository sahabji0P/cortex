"""
Gem log entity: everything of a gem, written down as it happens, in one list.

A gem's log is one list of entries, in one order (see `conversation_store.py`). Each
thing is written once, as the kind of entry it is, at the moment it is final. This is
where every entry gets its number, and the one place entries are written from.

Some kinds are what the gem remembers: what the user said, a line of ours, what the gem
said, a tool call and its result (`conversation_store.REMEMBERED`). Each holds one item
as a model is sent it, and the gem's conversation is those entries in order. A
conversation hands each one over the moment it joins its history (`Recording.remember`),
so the log's order is the order the model was sent things in.

The other kinds are what happened. No model is ever sent them. Each belongs to a turn
unless it says otherwise:

- `call.opened`, `call.closed`   a call began, or ended. No turn. Writing has neither.
- `turn.opened`      a reply was asked for. `by` says by what: the `user`, the `gem`
                     speaking first, or a `background_result` that is to be told.
                     `mode` is `call` or `chat`.
- `settings.changed` the model, the instructions or the tools differ from the gem's
                     last request. Only the parts that changed, each in full.
- `request.sent`     a request went to the model. It was sent everything the gem
                     remembered by then: every remembered entry before this one, and
                     nothing else. `count` is how many that is.
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
  never stored without its result.
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

from app.entities.conversation_store import (
    REMEMBERED,
    ConversationStoreClass,
    LogEntry,
    remembered,
    stamp,
)
from app.entities.history import Added, Mode
from app.entities.instructions import Layer
from app.entities.loop import ToolCall
from app.entities.model import MODELS, Item, ModelCall, ModelName, Usage
from app.entities.prices import PRICED, cost
from app.entities.tools import Tool

__all__ = ["Listener", "LogEntityClass", "Recording", "RequestRecord", "TurnRecord"]

logger = logging.getLogger(__name__)

# Is told each entry as it is written down: the gem's id, and the entry as a client is
# shown it.
type Listener = Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]]

# What opened a turn.
type OpenedBy = Literal["user", "gem", "background_result"]


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
        self.dropped = False


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
        # Asked together: this is waited for before a call opens.
        last, turn, request, changes = await asyncio.gather(
            self._store.last(gem_id),
            self._store.last(gem_id, "turn.opened"),
            self._store.last(gem_id, "request.sent"),
            self._store.of_kind(gem_id, "settings.changed"),
        )
        settings: dict[str, Any] = {}
        for changed in changes:
            settings.update(changed.data)
        # Another task may have read it, and counted on, while this one waited.
        self._gems.setdefault(
            gem_id,
            _Gem(
                seq=0 if last is None else last.seq + 1,
                turn=0 if turn is None or turn.turn is None else turn.turn,
                request=0 if request is None else int(request.data.get("request") or 0),
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

    def note(self, gem_id: ObjectId, kind: str, turn: int | None, data: dict[str, Any]) -> None:
        """Write down one thing that happened. It never raises, and never waits.

        Nothing is written of a gem whose log is not open: one that was deleted.
        """
        try:
            if gem_id not in self._gems:
                return
            (seq,) = self.take(gem_id)
            self._add(
                LogEntry(
                    gem_id=gem_id, kind=kind, seq=seq, at=datetime.now(UTC), turn=turn, data=data
                )
            )
        except Exception:
            logger.warning("a %s of gem %s could not be written down", kind, gem_id, exc_info=True)

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
        """Drop what the gem remembers and could not be stored. Return what was dropped.

        Whoever was holding the conversation has failed for it and will not try again.
        What comes next starts from what is stored, so these must not be stored later.
        """
        kept = self._gems.get(gem_id)
        if kept is None:
            return []
        dropped, kept.unstored, kept.fault = kept.unstored, [], None
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
            except Exception as exc:
                # What happened is lost. What the gem remembers is not: it is held, and
                # whoever waits for it to be stored is told (see `stored`).
                kept.unstored = [entry for entry in batch if entry.kind in REMEMBERED]
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
        self._settings: Callable[[], dict[str, Any]] | None = None

    def describe(
        self,
        *,
        model: ModelName,
        via: Callable[[], str],
        layers: Sequence[Layer],
        tools: Sequence[Tool],
    ) -> None:
        """Say what the conversation's requests are made with, to tell when it changes.

        `via` is asked at every request: the way to a provider can change while a call
        is open. The rest is fixed for the conversation.
        """
        self._model = model
        provider = next(info.provider for info in MODELS if info.name == model)
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

    def note(self, kind: str, turn: int | None, data: dict[str, Any]) -> None:
        if self._log is not None and self._gem_id is not None:
            self._log.note(self._gem_id, kind, turn, data)

    def request(self, turn: int | None, items: Sequence[Item], think: bool) -> "RequestRecord":
        """A request is about to be sent: write down what changed, and that it is sent.

        It is sent `items`, which are what the gem remembers by now: every one of them
        is in the log already, before this entry.
        """
        number = None
        with contextlib.suppress(Exception):
            if self._log is not None and self._gem_id is not None:
                if self._settings is not None:
                    changed = self._log.settings_changed(self._gem_id, self._settings())
                    if changed:
                        self.note("settings.changed", turn, changed)
                number = self._log.request_number(self._gem_id)
                self.note(
                    "request.sent",
                    turn,
                    {"request": number, "attempt": 1, "think": think, "count": len(items)},
                )
        return RequestRecord(self, turn, number, self._model)


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
