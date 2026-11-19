"""
Gem log entity: what happens with each gem, written down as it happens.

A gem's log holds two kinds of entry in one order (see `conversation_store.py`): the
items the gem remembers, which are what a model is sent, and what happened, which no
model is ever sent. This is where what happened is written down, and where every entry
gets its number.

What happened, by `kind`. Each belongs to a turn unless it says otherwise:

- `call.opened`, `call.closed`   a call began, or ended. No turn. Writing has neither.
- `turn.opened`      a reply was asked for. `by` says by what: the `user`, the `gem`
                     speaking first, or a `late_result`. `mode` is `call` or `chat`.
- `user.said`        the user's words as they arrived, typed or spoken.
- `note.written`     a line of ours was put in front of the model. `reason` says which
                     (see `history.Source`). The line that opens a call has no turn.
- `settings.changed` the model, the instructions or the tools differ from the gem's
                     last request. Only the parts that changed, each in full.
- `request.sent`     a request went to the model. `kept` and `added` say what it was
                     sent: the first `kept` items of the gem's request before it, then
                     `added`. `count` is how many that makes.
- `request.done`     how the request ended: `done`, `failed`, or `cut` while the model
                     was still producing. The text produced in full, the tool calls,
                     when the first of it came, the tokens charged for and their cost.
- `tool.started`, `tool.done`    a tool call ran. A call that outlives its reply ends
                     later, in order of time, and still names the turn that made it.
- `turn.closed`      the reply was settled into what the gem remembers: `finished`,
                     `cut` or `failed`, and for a cut reply the text that is kept.

Times: an entry's `at` is when it was written down, which is when it happened, with one
exception. A reply is settled when the next thing happens (in a call: when the user next
speaks, a late result is told, or the call ends), so `turn.closed` says when the reply
was settled, not when it stopped. When the model stopped is `request.done`. A reply
the user cuts into is settled at that instant, and its request is stopped after: so a
`turn.closed` can come before the `request.done` of the request it cut. Entries of one
turn are told apart by `turn`, not by where they fall.

Tokens: a request cut while the model was still producing is never told what it used.
Its usage and cost are None, which means unknown, not nothing.

Each gem's entries are numbered here, in one place: a reply being written is not the
only thing that adds to a log. A tool call that outlives its reply adds to it when
nobody holds the gem. A gem's numbers are read from the database the first time the gem
is used, and counted in memory from then on. This process is the only one that writes.

Writing down what happened never holds a reply up and never fails one. Each entry is
numbered and told to whoever listens at once, and written to the database behind the
reply's back, in order. Items are different: they are stored before the reply's last
frame, as they always were, and what happened before them is written first. If the
process stops, the last moment of what happened can be lost. An item cannot.
"""

import asyncio
import contextlib
import functools
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from bson import ObjectId

from app.entities.conversation_store import (
    ConversationItem,
    ConversationStoreClass,
    LogEntry,
    as_shown,
)
from app.entities.history import Mode, Source
from app.entities.instructions import Layer
from app.entities.loop import ToolCall
from app.entities.model import MODELS, Item, ModelCall, ModelName, Usage
from app.entities.prices import PRICED, cost
from app.entities.tools import Tool

__all__ = ["Listener", "LogEntityClass", "Recording", "RequestRecord", "TurnRecord"]

logger = logging.getLogger(__name__)

# Is told each entry as it is written down: the gem's id, and the entry as a client is
# shown it.
type Listener = Callable[[str, dict[str, Any]], Awaitable[None]]

# What opened a turn.
type OpenedBy = Literal["user", "gem", "late_result"]


class _Gem:
    """What is kept in memory of one gem's log."""

    def __init__(self, seq: int, turn: int, request: int, settings: dict[str, Any]) -> None:
        self.next = seq  # the next entry's number
        self.turn = turn  # the last turn's number
        self.request = request  # the last request's number
        # The model, instructions and tools as they were last written down.
        self.settings = settings
        # The items the gem's last request was sent, to say what the next one adds.
        self.sent: list[Item] = []
        # What happened and is not written to the database yet, in order.
        self.pending: list[LogEntry] = []
        self.writing: asyncio.Task[None] | None = None
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
        last = await self._store.last(gem_id)
        turn = await self._store.last(gem_id, "turn.opened")
        request = await self._store.last(gem_id, "request.sent")
        settings: dict[str, Any] = {}
        for changed in await self._store.of_kind(gem_id, "settings.changed"):
            settings.update(_data(changed.shown()))
        # Another task may have read it, and counted on, while this one waited.
        self._gems.setdefault(
            gem_id,
            _Gem(
                seq=0 if last is None else last.seq + 1,
                turn=0 if turn is None or turn.turn is None else turn.turn,
                request=0 if request is None else int(request.shown().get("request", 0)),
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

    def recording(self, gem_id: ObjectId, mode: Mode) -> "Recording":
        """What one conversation with the gem, in `mode`, writes down. The log must be open."""
        return Recording(self, gem_id, mode)

    def note(self, gem_id: ObjectId, kind: str, turn: int | None, data: dict[str, Any]) -> None:
        """Write down one thing that happened. It never raises, and never waits.

        Nothing is written of a gem whose log is not open: one that was deleted.
        """
        try:
            kept = self._gems.get(gem_id)
            if kept is None:
                return
            (seq,) = self.take(gem_id)
            entry = LogEntry(
                gem_id=gem_id, kind=kind, seq=seq, at=datetime.now(UTC), turn=turn, data=data
            )
            kept.pending.append(entry)
            self._tell(gem_id, as_shown(entry.seq, entry.at, entry.kind, entry.turn, entry.data))
            if kept.writing is None or kept.writing.done():
                kept.writing = asyncio.get_running_loop().create_task(self._write(gem_id, kept))
        except Exception:
            logger.warning("a %s of gem %s could not be written down", kind, gem_id, exc_info=True)

    def stored(self, items: Sequence[ConversationItem]) -> None:
        """Items have been stored: tell whoever listens."""
        for item in items:
            if item.seq is not None:
                self._tell(
                    item.gem_id, as_shown(item.seq, item.at, "item", None, _item_fields(item))
                )

    async def written(self, gem_id: ObjectId | None = None) -> None:
        """Wait until what happened so far is in the database: of one gem, or of every gem."""
        for key, kept in list(self._gems.items()):
            if gem_id is not None and key != gem_id:
                continue
            while kept.writing is not None and not kept.writing.done():
                await asyncio.wait({kept.writing})

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

    def sent(self, gem_id: ObjectId, items: Sequence[Item]) -> tuple[int, list[Item]]:
        """What a request adds to the gem's last: how many items it keeps, and the rest."""
        kept = self._gems.get(gem_id)
        if kept is None:
            return 0, list(items)
        same = 0
        for before, now in zip(kept.sent, items, strict=False):
            if before != now:
                break
            same += 1
        kept.sent = list(items)
        return same, list(items[same:])

    async def _write(self, gem_id: ObjectId, kept: _Gem) -> None:
        while kept.pending:
            batch, kept.pending = kept.pending, []
            try:
                await self._store.note(batch)
            except Exception:
                # What happened is lost. The gem's replies and its items are not.
                logger.warning(
                    "%d entries of gem %s's log could not be written", len(batch), gem_id
                )
            if kept.dropped:
                # The gem was deleted while this was being written: it goes too.
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


def _data(entry: dict[str, Any]) -> dict[str, Any]:
    """An entry's own fields, without the ones every entry has."""
    return {key: value for key, value in entry.items() if key not in ("seq", "at", "kind", "turn")}


def _item_fields(item: ConversationItem) -> dict[str, Any]:
    fields = {
        "place": item.place,
        "item": item.item,
        "source": item.source,
        "mode": item.mode,
        "mark": item.mark,
        "shown": item.shown,
    }
    return {key: value for key, value in fields.items() if value is not None}


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


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

    def line(self, source: Source, text: str, turn: "TurnRecord | None" = None) -> None:
        """A line of ours was put in front of the model."""
        number = None if turn is None else turn.number
        self.note("note.written", number, {"reason": source, "text": text})

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
        """A request is about to be sent: write down what changed, and what it is sent."""
        number = None
        with contextlib.suppress(Exception):
            if self._log is not None and self._gem_id is not None:
                if self._settings is not None:
                    changed = self._log.settings_changed(self._gem_id, self._settings())
                    if changed:
                        self.note("settings.changed", turn, changed)
                number = self._log.request_number(self._gem_id)
                kept, added = self._log.sent(self._gem_id, items)
                self.note(
                    "request.sent",
                    turn,
                    {
                        "request": number,
                        "attempt": 1,
                        "think": think,
                        "kept": kept,
                        "added": added,
                        "count": len(items),
                    },
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

    def said(self, text: str) -> None:
        """The user's words arrived."""
        self._recording.note("user.said", self.number, {"text": text, "mode": self._recording.mode})

    def line(self, source: Source, text: str) -> None:
        self._recording.line(source, text, self)

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
                    "arguments": call.arguments,
                    "connection": None if call.tool is None else call.tool.connection,
                },
            )
            if call.running is not None:
                call.running.add_done_callback(functools.partial(self._tool_done, call))

    def failed(self, error: BaseException) -> None:
        """The reply failed. Said when its turn is closed."""
        self._error = str(error) or type(error).__name__

    def closed(self, *, finished: bool, kept: str | None) -> None:
        """The reply is settled. `kept` is the text kept of a cut reply, None if it was not cut."""
        if self._closed:
            return
        self._closed = True
        if self._error is not None:
            outcome = "failed"
        elif finished and kept is None:
            outcome = "finished"
        else:
            outcome = "cut"
        self._recording.note(
            "turn.closed", self.number, {"outcome": outcome, "kept": kept, "error": self._error}
        )

    def _tool_done(self, call: ToolCall, ran: asyncio.Task[str]) -> None:
        with contextlib.suppress(Exception):
            if ran.cancelled():
                outcome, result = "cancelled", None
            elif ran.exception() is not None:
                outcome, result = "failed", str(ran.exception())
            else:
                outcome, result = ("failed" if call.failed else "done"), ran.result()
            self._recording.note(
                "tool.done",
                self.number,
                {
                    "call_id": call.call_id,
                    "outcome": outcome,
                    "result": result,
                    # Whether its reply was over before it was: it was left running.
                    "late": call.mark == "placeholder",
                },
            )


class RequestRecord:
    """What is written down of one request to a model, from when it is sent."""

    def __init__(
        self, recording: Recording, turn: int | None, number: int | None, model: ModelName | None
    ) -> None:
        self._recording = recording
        self._turn = turn
        self.number = number
        self._model = model
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
        with contextlib.suppress(Exception):
            priced = None if self._model is None else cost(self._model, usage)
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
