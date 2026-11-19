"""
Conversation entity: each gem's one conversation, and who is adding to it.

A gem is one entity with one conversation, whether the user calls it or writes to it.
The database is the truth (see `conversation_store.py`): every written reply and every
call starts from what is stored, and what each adds is stored after it, in order. What
is kept in memory, for each gem, is a `GemConversation`:

- who is adding to the conversation now: one writer at a time, a reply being written or
  a call. A call holds the gem from when it starts opening until what it added is
  stored; while it does, nothing is written to the gem.
- the gem's tool calls that outlived their replies, until their results are told (see
  `late_results.py`): one `LateResults` for the gem, whoever made the call. A result is
  told in the call if one holds the gem, and in writing otherwise,
- the user's written turn that no reply has got through to yet (see `history.py`).

A gem with none of these is not kept. Running calls live in this process's memory, so a
restart loses them. A placeholder whose call is gone is closed with a stub saying so,
before the next reply is made.
"""

import asyncio
import contextlib
import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from bson import ObjectId

from app.core.errors import NotFoundError
from app.entities.conversation_store import ConversationItem, ConversationStoreClass
from app.entities.gem_ent import GemEntityClass
from app.entities.gem_log import LogEntityClass, Recording
from app.entities.history import Added, Mode, UserTurn, stubs
from app.entities.late_results import LateResult, LateResults, delivers, late_stub
from app.entities.loop import ToolCall
from app.entities.model import Item

__all__ = [
    "Caller",
    "ConversationEntityClass",
    "GemConversation",
    "Message",
    "Stored",
    "Writer",
    "gem_key",
]

# The result a call lost to a restart is given, so its placeholder does not promise a
# result for ever. THE OWNER HAS NOT RULED ON THIS WORDING.
_LOST = (
    "This call was lost before it finished, and its result will not arrive. Make it again "
    "if it is still needed."
)


@dataclass(frozen=True, slots=True)
class Message:
    """One message of a gem's conversation, as it is shown."""

    role: Literal["user", "gem", "tool"]
    text: str
    at: datetime


class Writer(Protocol):
    """A reply being written to a gem, which holds the gem until it is stored."""

    # Set once the reply is stored and its last frame sent (or could not be).
    over: asyncio.Event

    def cut(self) -> None:
        """Cut the reply off where it is."""
        ...


class Caller(Protocol):
    """A call with a gem, which holds the gem from its opening until it is stored."""

    def results_came(self) -> None:
        """A late result has come in, to be told in the call."""
        ...


@dataclass(frozen=True, slots=True)
class Stored:
    """A gem's conversation as it is stored: its items, and the mode of the last."""

    items: list[Item]
    # "chat" for an empty conversation: it counts as written.
    mode: Mode = "chat"


class GemConversation:
    """What is kept in memory of one gem's conversation."""

    def __init__(self, written: Callable[[], None]) -> None:
        # The gem's calls that outlived their replies, until their results are told. A
        # result that comes in is told in the call that holds the gem, or else by
        # `written`, in writing.
        self.late = LateResults(self._results_came)
        self._written = written
        # The reply being written to the gem, until it is stored: one at a time.
        self.writer: Writer | None = None
        # The call that holds the gem, from its opening until it is stored.
        self.call: Caller | None = None
        # Messages whose replies were cut or failed before they left anything in the
        # history. The next message joins them, and they are stored as one.
        self.turn = UserTurn()

    @property
    def idle(self) -> bool:
        """Whether nothing of it needs keeping."""
        held = self.writer is not None or self.call is not None
        return not held and not self.late.pending and not self.turn.open

    def _results_came(self) -> None:
        if self.call is not None:
            self.call.results_came()
        else:
            self._written()


class ConversationEntityClass:
    def __init__(
        self,
        *,
        gems: GemEntityClass,
        store: ConversationStoreClass,
        log: LogEntityClass | None = None,
    ) -> None:
        self._gems = gems
        self._store = store
        # Hands out the numbers of each gem's log. Left out, the conversations keep
        # their own.
        self.log = LogEntityClass(store) if log is None else log
        # A gem that is deleted takes its log with it: nothing more is written of it.
        gems.when_deleted(self.log.drop)
        # What is kept of each gem's conversation, by `gem_key`.
        self._kept: dict[str, GemConversation] = {}
        self._quiet: Callable[[str], None] = _nobody

    def tell_results_with(self, quiet: Callable[[str], None]) -> None:
        """Who tells a gem's late results: `quiet` is given the gem's key when one comes in.

        That is chat: it writes the gem a reply about them, once nothing else is.
        """
        self._quiet = quiet

    def gem(self, key: str) -> GemConversation:
        """What is kept of a gem's conversation, made if there is none yet."""
        kept = self._kept.get(key)
        if kept is None:
            kept = self._kept[key] = GemConversation(lambda: self._quiet(key))
        return kept

    def find(self, key: str) -> GemConversation | None:
        """What is kept of a gem's conversation, if anything is."""
        return self._kept.get(key)

    def kept(self) -> Iterable[GemConversation]:
        return list(self._kept.values())

    def let_go(self, key: str) -> None:
        """Keep nothing for a gem whose conversation needs nothing kept."""
        kept = self._kept.get(key)
        if kept is not None and kept.idle:
            del self._kept[key]

    def call_ended(self, key: str, call: Caller) -> None:
        """A call that held the gem has ended, and what it added is stored.

        The gem may be written to again, and late results waiting are told in writing.
        """
        kept = self._kept.get(key)
        if kept is None or kept.call is not call:
            return
        kept.call = None
        self._quiet(key)
        self.let_go(key)

    async def messages(self, gem_id: str) -> list[Message]:
        """A gem's conversation as it is shown, oldest first.

        Raises `NotFoundError` if there is no such gem. A gem whose config cannot be
        read still has its conversation shown.
        """
        if not await self._gems.exists(gem_id):
            raise NotFoundError("Gem")
        shown: list[Message] = []
        for stored in await self._store.load(ObjectId(gem_id)):
            item = stored.item
            if "role" in item:
                if item["role"] == "user":
                    shown.append(Message("user", item["content"], stored.at))
                elif item["role"] == "assistant":
                    shown.append(Message("gem", item["content"], stored.at))
            # Stubs and cancels are stored with nothing to show.
            elif item["type"] == "function_call" and stored.shown is not None:
                shown.append(Message("tool", stored.shown, stored.at))
        return shown

    async def load(self, gem_id: ObjectId, *, told: Sequence[LateResult] = ()) -> Stored:
        """The gem's conversation as it is stored, with the calls lost to a restart closed.

        Each lost call is closed with a stub, stored after the conversation in the mode
        of its last item: closing it changes nothing about how the user is talking.
        `told` are late results about to be added, whose calls are not lost.
        """
        # Whoever reads the conversation is about to add to the gem's log.
        await self.log.open(gem_id)
        found = await self._store.load(gem_id)
        stored = Stored([entry.item for entry in found], found[-1].mode if found else "chat")
        lost = self._lost(str(gem_id), found, told)
        return await self.add(gem_id, stored, stubs(lost, "lost-call"), stored.mode)

    async def add(
        self, gem_id: ObjectId, stored: Stored, added: Sequence[Added], mode: Mode
    ) -> Stored:
        """Store items after the conversation, all or none, as added in `mode`.

        Returns the conversation with them.
        """
        if not added:
            return stored
        await self.log.open(gem_id)
        numbers = self.log.take(gem_id, len(added))
        items = [
            ConversationItem(
                gem_id=gem_id,
                seq=seq,
                place=len(stored.items) + offset,
                at=one.at,
                item=one.item,
                source=one.source,
                turn=one.turn,
                shown=one.shown,
                mark=one.mark,
                mode=mode,
            )
            for offset, (seq, one) in enumerate(zip(numbers, added, strict=True))
        ]
        # What happened before them is written first, so the log is in order on disk.
        await self.log.written(gem_id)
        await self._store.add(items)
        # Telling whoever watches is no part of storing: it cannot fail what is stored.
        with contextlib.suppress(Exception):
            self.log.stored(items)
        return Stored([*stored.items, *(one.item for one in added)], mode)

    async def log_page(
        self, gem_id: str, *, before: int | None = None, limit: int = 200
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """A page of a gem's log as it is shown, oldest first, and the settings before it.

        The latest page, or with `before` the entries just before that number.
        Everything is in it, in the order it happened: what the gem remembers and what
        happened (see `gem_log.py`). With it come the model, instructions and tools in
        force just before the page's first entry, so that a change inside the page can
        be shown against what it changed. Raises `NotFoundError` if there is no such gem.
        """
        if not await self._gems.exists(gem_id):
            raise NotFoundError("Gem")
        found = ObjectId(gem_id)
        # What has just happened is on its way to the database: it is waited for.
        await self.log.written(found)
        entries = [
            line.shown() for line in await self._store.page(found, before=before, limit=limit)
        ]
        first = entries[0]["seq"] if entries else 0
        return entries, await self._settings(found, upto=first)

    async def log_sent(self, gem_id: str, seq: int) -> dict[str, Any]:
        """Everything the request written down as entry `seq` was sent.

        Its items, worked out from what each of the gem's requests added to the one
        before it, and the model, instructions and tools in force. `exact` is False if
        the items do not come to the count the request wrote down: an entry before it
        was lost. Raises `NotFoundError` if `seq` is not a request of this gem.
        """
        if not await self._gems.exists(gem_id):
            raise NotFoundError("Gem")
        found = ObjectId(gem_id)
        await self.log.written(found)
        items: list[Any] = []
        request: dict[str, Any] | None = None
        for line in await self._store.of_kind(found, "request.sent"):
            if line.seq > seq:
                break
            request = line.shown()
            items = [*items[: request.get("kept", 0)], *request.get("added", [])]
        if request is None or request["seq"] != seq:
            raise NotFoundError("Request")
        return {
            "request": request.get("request"),
            "items": items,
            "exact": len(items) == request.get("count"),
            **await self._settings(found, upto=seq),
        }

    async def _settings(self, gem_id: ObjectId, *, upto: int) -> dict[str, Any]:
        """The model, instructions and tools in force before entry `upto`, as last written."""
        parts: dict[str, Any] = {"model": None, "instructions": None, "tools": None}
        for line in await self._store.of_kind(gem_id, "settings.changed"):
            if line.seq >= upto:
                break
            changed = line.shown()
            parts.update({part: changed[part] for part in parts if part in changed})
        return parts

    async def log_entry(self, gem_id: str, seq: int) -> dict[str, Any]:
        """One entry of a gem's log, in full. Raises `NotFoundError` if there is none."""
        if not await self._gems.exists(gem_id):
            raise NotFoundError("Gem")
        found = ObjectId(gem_id)
        await self.log.written(found)
        line = await self._store.one(found, seq)
        if line is None:
            raise NotFoundError("Log entry")
        return line.shown()

    def recording(self, gem_id: ObjectId, mode: Mode) -> Recording:
        """What a conversation with the gem writes down of what happens (see `gem_log.py`).

        The gem's conversation must have been read first (`load`).
        """
        return self.log.recording(gem_id, mode)

    async def gone(self, gem_id: ObjectId) -> bool:
        """Whether the gem has been deleted. If it has, what was stored since goes too.

        A gem's conversation goes with it, so what was added while it was being deleted
        would otherwise be left behind.
        """
        if await self._gems.exists(str(gem_id)):
            return False
        self.log.drop(gem_id)
        await self._store.forget(gem_id)
        return True

    def _lost(
        self, key: str, stored: Sequence[ConversationItem], told: Sequence[LateResult]
    ) -> list[ToolCall]:
        """A stub for each call lost to a restart, closing its placeholder.

        Lost is a placeholder whose call is not running or waiting here (or being `told`
        now), that no stub has delivered, and that no `cancel_tool_call` stopped. Each is
        known by the mark stored with its result, never by the result's wording.
        """
        kept = self._kept.get(key)
        here = {late.call.call_id for late in told}
        items = [entry.item for entry in stored]
        calls = {
            item["call_id"]: item
            for item in items
            if "type" in item and item["type"] == "function_call"
        }
        delivered = {original for item in items if (original := delivers(item))}
        placeholders: list[str] = []
        cancelled: set[str | None] = set()
        for entry in stored:
            item = entry.item
            if "type" not in item or item["type"] != "function_call_output":
                continue
            if entry.mark == "placeholder":
                placeholders.append(item["call_id"])
            elif entry.mark == "cancelled" and item["call_id"] in calls:
                cancelled.add(_named(calls[item["call_id"]]["arguments"]))
        lost: list[ToolCall] = []
        for call_id in placeholders:
            if call_id not in calls or call_id in here or call_id in delivered:
                continue
            if call_id in cancelled or (kept is not None and kept.late.holds(call_id)):
                continue
            call = calls[call_id]
            lost.append(late_stub(ToolCall(call_id, call["name"], call["arguments"]), _LOST))
        return lost


def gem_key(gem_id: str) -> str:
    """The same gem by whichever spelling of its id. One that is not an id is kept as is."""
    return str(ObjectId(gem_id)) if ObjectId.is_valid(gem_id) else gem_id


def _named(arguments: str) -> str | None:
    """The call_id a `cancel_tool_call` named, if it can be read."""
    try:
        named = json.loads(arguments)["call_id"]
    except (ValueError, KeyError, TypeError):
        return None
    return str(named)


def _nobody(key: str) -> None:
    pass
