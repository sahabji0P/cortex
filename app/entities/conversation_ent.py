"""
Conversation entity: each gem's one conversation, and who is adding to it.

A gem is one entity with one conversation, whether the user calls it or writes to it.
The database is the truth (see `conversation_store.py`): every written reply and every
call starts from what the gem's log remembers, and what each adds is remembered after
it, in order, the moment it joins the history (see `gem_log.py`). What is kept in
memory, for each gem, is a `GemConversation`:

- who is adding to the conversation now: one writer at a time, a reply being written or
  a call. A call holds the gem from when it starts opening until what it added is
  stored; while it does, nothing is written to the gem.
- the gem's tool calls that outlived their replies, until their results are told (see
  `late_results.py`): one `LateResults` for the gem, whoever made the call. A result is
  told in the call if one holds the gem, and in writing otherwise.
- the events that came in for the gem's watches, until they are told (see
  `pending_events.py`): one `PendingEvents` for the gem. Like a result, an event is told
  in the call if one holds the gem, and in writing otherwise, and it wakes a gem that is
  quiet. They are written down in the log as they come, so what waits is read again from
  it when needed.
- a compaction set to run before the gem's cache expires, until it runs (see
  `compaction_ent.py`). Anything that takes the gem first stops it.

The user can reset a gem (`reset`): it starts again, knowing nothing from before. A
reset takes the gem as a reply does, so a message sent meanwhile is answered by the
gem as it is after the reset. Events still waiting are dropped with the rest, and
whoever asked is told of the reset (`when_reset`), as the gem's watches are.

Whoever asks is told each time a gem is let go after a reply, a call, a reset or a
compaction (`when_settled`): what the gem would be sent next has changed.

Every reply in writing, and every call when it starts, checks first whether the
conversation starts cold, and is sent it as the latest cold start has it sent (`sent`,
see `cold_start.py`), and as the compaction in force has it sent: what came before its
line is sent as its summary (see `compaction.py`).

A gem with none of these is not kept. Running calls live in this process's memory, so a
restart loses them. A placeholder whose call is gone is closed with a stub saying so,
before the next reply is made.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from bson import ObjectId

from app.core.errors import ConflictError, NotFoundError
from app.entities.cold_start import CACHE_LIFETIME, Cache, Numbered, cold
from app.entities.compaction import Compacted, asked, view
from app.entities.conversation_store import EVENT_RECEIVED, ConversationStoreClass, Remembered
from app.entities.gem_ent import GemEntityClass
from app.entities.gem_log import LogEntityClass, Recording
from app.entities.history import Added, Mode, stubs
from app.entities.late_results import LateResult, LateResults, delivers, late_stub
from app.entities.loop import ToolCall
from app.entities.model import Item
from app.entities.pending_events import PendingEvent, PendingEvents, pending

__all__ = [
    "Caller",
    "ConversationEntityClass",
    "GemConversation",
    "Message",
    "Settled",
    "Stored",
    "Writer",
    "gem_key",
    "numbered",
]

# What a gem was let go after.
type Settled = Literal["reply", "call", "reset", "compaction"]

# The result a call lost to a restart is given, so its placeholder does not promise a
# result for ever. THE OWNER HAS NOT RULED ON THIS WORDING.
_LOST = (
    "This call was lost before it finished, and its result will not arrive. Make it again "
    "if it is still needed."
)


@dataclass(frozen=True, slots=True)
class Message:
    """One message of a gem's conversation, as it is shown.

    An event the gem was told is a message of its own, `event`: it is not the user's.
    """

    role: Literal["user", "gem", "tool", "event"]
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
        """A late result, or an event, has come in, to be told in the call."""
        ...


class _Resetting:
    """A reset of a gem, which holds the gem as a reply does until it is done."""

    def __init__(self) -> None:
        self.over = asyncio.Event()

    def cut(self) -> None:
        """A reset is not cut: whoever comes next waits for it."""


@dataclass(frozen=True, slots=True)
class Stored:
    """A gem's conversation as it is stored: what it remembers, and the mode of the last."""

    # Each item with its number and what decides how it is sent. One added after the
    # conversation was read has no number: it comes after every request so far.
    entries: list[Numbered]
    # "chat" for an empty conversation: it counts as written.
    mode: Mode = "chat"
    # When the last line of ours that gave the time was written, if one was.
    last_time: datetime | None = None
    # Where the gem's cache stood when the conversation was read (see `cold_start.py`).
    cache: Cache = field(default_factory=Cache)
    # The compaction in force when the conversation was read, if any (see `compaction.py`).
    compacted: Compacted | None = None

    @property
    def items(self) -> list[Item]:
        """The items as they are stored."""
        return [entry.item for entry in self.entries]


class GemConversation:
    """What is kept in memory of one gem's conversation."""

    def __init__(self, written: Callable[[], None]) -> None:
        # The gem's calls that outlived their replies, until their results are told. A
        # result that comes in is told in the call that holds the gem, or else by
        # `written`, in writing.
        self.late = LateResults(self._came)
        # The events that came in for the gem's watches, until they are told: told, and
        # the gem woken, in the same way as a late result.
        self.events = PendingEvents(self._came)
        self._written = written
        # A compaction set to run before the gem's cache expires, until it runs.
        self.auto: asyncio.Task[None] | None = None
        self._writer: Writer | None = None
        self._call: Caller | None = None

    @property
    def writer(self) -> Writer | None:
        """The reply being written to the gem, until it is stored: one at a time."""
        return self._writer

    @writer.setter
    def writer(self, writer: Writer | None) -> None:
        # Whatever takes the gem stops a compaction that was set to run.
        if writer is not None:
            self.stop_auto()
        self._writer = writer

    @property
    def call(self) -> Caller | None:
        """The call that holds the gem, from its opening until it is stored."""
        return self._call

    @call.setter
    def call(self, call: Caller | None) -> None:
        if call is not None:
            self.stop_auto()
        self._call = call

    def stop_auto(self) -> None:
        """Stop the compaction set to run before the cache expires, if one is."""
        if self.auto is not None:
            self.auto.cancel()
            self.auto = None

    @property
    def idle(self) -> bool:
        """Whether nothing of it needs keeping."""
        held = self.writer is not None or self.call is not None or self.auto is not None
        return not held and not self.late.pending and not self.events.pending

    def _came(self) -> None:
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
        # And the events that wait for it, which are in that log.
        gems.when_deleted(self._forget_events)
        # What is kept of each gem's conversation, by `gem_key`.
        self._kept: dict[str, GemConversation] = {}
        self._quiet: Callable[[str], None] = _nobody
        # Told the id of each gem that is reset, once the reset is stored.
        self._resets: list[Callable[[ObjectId], Awaitable[None]]] = []
        # Told the key of each gem that is let go, and after what.
        self._settled: list[Callable[[str, Settled], None]] = []

    def tell_results_with(self, quiet: Callable[[str], None]) -> None:
        """Who tells a gem's late results: `quiet` is given the gem's key when one comes in.

        That is chat: it writes the gem a reply about them, once nothing else is.
        """
        self._quiet = quiet

    def when_reset(self, told: Callable[[ObjectId], Awaitable[None]]) -> None:
        """Tell `told` each gem that is reset, once the reset is stored.

        Such as the gem's watches, which end with it: what they were for is forgotten.
        The reset is over only once each is done, and fails if one raises.
        """
        self._resets.append(told)

    def when_settled(self, told: Callable[[str, Settled], None]) -> None:
        """Tell `told` each gem that is let go after a reply, a call, a reset or a compaction.

        It is told the gem's key, at once: it must not raise, and must not wait.
        """
        self._settled.append(told)

    def settled(self, key: str, after: Settled) -> None:
        """The gem is let go after `after`: tell whoever asked."""
        for told in self._settled:
            told(key, after)

    def wake(self, key: str) -> None:
        """Tell the gem's late results and events that wait, if nothing holds it."""
        self._quiet(key)

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
        self.settled(key, "call")
        self.let_go(key)

    async def reset(self, gem_id: str) -> None:
        """Reset a gem: from here on, it knows nothing from before. Its config stays.

        A reply being written is cut and stored first, and calls still running in the
        background are stopped, their results dropped: what they would answer is gone.
        Events still waiting are dropped too: the gem knows nothing of them. The log
        keeps everything: the reset is an entry of its own. Raises
        `NotFoundError` if there is no such gem, `ConflictError` while a call holds it,
        and what stopped the reset if it could not be stored.
        """
        if not await self._gems.exists(gem_id):
            raise NotFoundError("Gem")
        key, found = gem_key(gem_id), ObjectId(gem_id)
        kept = self.gem(key)
        if kept.call is not None:
            raise ConflictError("the gem is in a call")
        resetting = _Resetting()
        previous, kept.writer = kept.writer, resetting
        try:
            if previous is not None:
                previous.cut()
                await previous.over.wait()
            await self.log.open(found)
            reset = await self.log.record_reset(found)
            # Only once the reset is stored. A result that comes meanwhile is not told:
            # the reset holds the gem. An event that came after the reset is kept.
            kept.late.stop()
            kept.events.drop_before(reset)
            for told in self._resets:
                await told(found)
        finally:
            resetting.over.set()
            if kept.writer is resetting:
                kept.writer = None
                # An event that came after the reset, while it held the gem, wakes it now.
                self._quiet(key)
            self.settled(key, "reset")
            self.let_go(key)

    async def messages(self, gem_id: str) -> list[Message]:
        """A gem's conversation as it is shown, oldest first.

        Raises `NotFoundError` if there is no such gem. A gem whose config cannot be
        read still has its conversation shown.
        """
        if not await self._gems.exists(gem_id):
            raise NotFoundError("Gem")
        found = ObjectId(gem_id)
        # What has just been added is on its way to the database: it is waited for.
        await self.log.written(found)
        shown: list[Message] = []
        for stored in await self._store.load(found):
            item = stored.data.item
            if stored.kind == EVENT_RECEIVED:
                # Told as a user message, but not the user's: shown as what came in.
                shown.append(Message("event", stored.data.shown or "", stored.at))
            elif "role" in item:
                if item["role"] == "user":
                    shown.append(Message("user", item["content"], stored.at))
                elif item["role"] == "assistant":
                    shown.append(Message("gem", item["content"], stored.at))
            # Stubs and cancels are stored with nothing to show.
            elif item["type"] == "function_call" and stored.data.shown is not None:
                shown.append(Message("tool", stored.data.shown, stored.at))
        return shown

    async def load(self, gem_id: ObjectId, *, told: Sequence[LateResult] = ()) -> Stored:
        """The gem's conversation as it is stored, with the calls lost to a restart closed.

        Each lost call is closed with a stub, stored after the conversation in the mode
        of its last item: closing it changes nothing about how the user is talking.
        `told` are late results about to be added, whose calls are not lost.
        """
        # Whoever reads the conversation is about to add to the gem's log.
        await self.log.open(gem_id)
        # What was added last is on its way to the database: it is waited for.
        await self.log.written(gem_id)
        kept = self._kept.get(str(gem_id))
        if kept is not None and kept.events.stale:
            # Events taken to be told could not be stored: they wait in the log.
            kept.events.refresh(await self.pending_events(gem_id))
        found = await self._store.load(gem_id)
        mode = found[-1].data.mode if found else "chat"
        compacted = await self._store.compacted(gem_id)
        since = 0 if compacted is None else compacted.through
        # The last line that gave the time that the model is still sent whole: one a
        # summary stands for is not.
        last_time = next(
            (e.at for e in reversed(found) if e.data.source == "time-line" and e.seq >= since),
            None,
        )
        stored = Stored(
            numbered(found), mode, last_time, await self._store.cache(gem_id), compacted
        )
        lost = self._lost(str(gem_id), found, told)
        return await self.add(gem_id, stored, stubs(lost, "lost-call"), stored.mode)

    async def pending_events(self, gem_id: ObjectId) -> list[PendingEvent]:
        """The events the gem's log says wait to be told, in the order they came."""
        return [pending(entry) for entry in await self._store.pending_events(gem_id)]

    async def add(
        self, gem_id: ObjectId, stored: Stored, added: Sequence[Added], mode: Mode
    ) -> Stored:
        """Add items after the conversation, as added in `mode`, by nobody's reply.

        Such as the stub that closes a lost call, or a late result told in writing. What
        a conversation adds itself, it hands to the log as it goes (`Recording.remember`).
        Returns the conversation with them, once they are stored: raises if they could
        not be, and then none of them is.
        """
        if not added:
            return stored
        await self.log.open(gem_id)
        record = self.log.recording(gem_id, mode)
        for one in added:
            record.remember(one)
        await self.stored(gem_id)
        times = [one.at for one in added if one.source == "time-line"]
        last_time = times[-1] if times else stored.last_time
        numbered = [Numbered(None, one.item, one.mark, one.delivers) for one in added]
        return Stored([*stored.entries, *numbered], mode, last_time, stored.cache, stored.compacted)

    def sent(self, record: Recording, stored: Stored, *, changed: bool) -> list[Item]:
        """The conversation as the model is to be sent it, once it is checked for a cold start.

        Asked once for each reply in writing and once when a call starts, right after
        `record` is described: `changed` is what that said. A cold start is written down
        (`cache.cold`), and must be stored like what the gem remembers. Long results
        before its line are sent expired, as they are after the latest cold start before
        it (see `cold_start.py`). With a compaction in force, what came before its line
        is sent as its summary (see `compaction.py`).
        """
        before = stored.cache.before
        if record.provider is not None and stored.cache.sent is not None:
            reason = cold(stored.cache, record.provider, changed=changed, now=datetime.now(UTC))
            if reason is not None:
                before = stored.cache.sent
                record.cold(reason, before)
        return view(stored.entries, before, stored.compacted)

    async def stored(self, gem_id: ObjectId) -> None:
        """Wait until what the gem remembers so far is stored.

        Raises what stopped it, if it could not be. What could not be stored is then
        dropped: whoever asked has failed for it, and what comes next starts from what
        is stored.
        """
        try:
            await self.log.stored(gem_id)
        except BaseException:
            self.log.give_up(gem_id)
            raise

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
            entry.shown() for entry in await self._store.page(found, before=before, limit=limit)
        ]
        first = entries[0]["seq"] if entries else 0
        return entries, await self._settings(found, upto=first)

    async def log_sent(self, gem_id: str, seq: int) -> dict[str, Any]:
        """Everything the request written down as entry `seq` was sent.

        Its items are what the gem remembered by then: every remembered entry before
        the request's own since the gem was last reset, in order, as the latest cold
        start and the latest compaction before it had them sent (see `cold_start.py` and
        `compaction.py`). A compaction's own request was sent one more line of ours,
        the one that asks for the summary. With them come the model, instructions and
        tools in force. `exact` is False if the items do not come to the count the
        request wrote down: an entry before it was lost. Raises `NotFoundError` if `seq`
        is not a request of this gem.
        """
        if not await self._gems.exists(gem_id):
            raise NotFoundError("Gem")
        found = ObjectId(gem_id)
        await self.log.written(found)
        request = await self._store.one(found, seq)
        if request is None or request.kind != "request.sent":
            raise NotFoundError("Request")
        remembered = await self._store.load(found, before=seq)
        cache = await self._store.cache(found, before=seq)
        compacted = await self._store.compacted(found, before=seq)
        items = view(numbered(remembered), cache.before, compacted)
        if request.turn is not None:
            opened = await self._store.opened(found, request.turn)
            if opened is not None and opened.data.get("by") == "compaction":
                items.append(asked())
        return {
            "request": request.data.get("request"),
            "items": items,
            "exact": len(items) == request.data.get("count"),
            **await self._settings(found, upto=seq),
        }

    async def cache_until(self, gem_id: ObjectId, cache: Cache) -> datetime | None:
        """When the gem's cache expires: its last request's time and its provider's lifetime.

        The provider is the one of the model in force when that request was sent. None
        if no request was sent since the gem was last reset.
        """
        if cache.sent is None or cache.sent_at is None:
            return None
        model = (await self._settings(gem_id, upto=cache.sent))["model"]
        if model is None or model.get("provider") not in CACHE_LIFETIME:
            return None
        return cache.sent_at + CACHE_LIFETIME[model["provider"]]

    async def _settings(self, gem_id: ObjectId, *, upto: int) -> dict[str, Any]:
        """The model, instructions and tools in force before entry `upto`, as last written."""
        parts: dict[str, Any] = {"model": None, "instructions": None, "tools": None}
        for changed in await self._store.of_kind(gem_id, "settings.changed"):
            if changed.seq >= upto:
                break
            parts.update({part: changed.data[part] for part in parts if part in changed.data})
        return parts

    async def log_entry(self, gem_id: str, seq: int) -> dict[str, Any]:
        """One entry of a gem's log, in full. Raises `NotFoundError` if there is none."""
        if not await self._gems.exists(gem_id):
            raise NotFoundError("Gem")
        found = ObjectId(gem_id)
        await self.log.written(found)
        entry = await self._store.one(found, seq)
        if entry is None:
            raise NotFoundError("Log entry")
        return entry.shown()

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
        self, key: str, stored: Sequence[Remembered], told: Sequence[LateResult]
    ) -> list[ToolCall]:
        """A stub for each call lost to a restart, closing its placeholder.

        Lost is a placeholder whose call is not running or waiting here (or being `told`
        now), that no stub has delivered, and that no `cancel_tool_call` stopped. Each is
        known by the mark stored with its result, never by the result's wording.
        """
        kept = self._kept.get(key)
        here = {late.call.call_id for late in told}
        items = [entry.data.item for entry in stored]
        calls = {
            item["call_id"]: item
            for item in items
            if "type" in item and item["type"] == "function_call"
        }
        delivered = {original for item in items if (original := delivers(item))}
        placeholders: list[str] = []
        cancelled: set[str | None] = set()
        for entry in stored:
            item = entry.data.item
            if "type" not in item or item["type"] != "function_call_output":
                continue
            if entry.data.mark == "placeholder":
                placeholders.append(item["call_id"])
            elif entry.data.mark == "cancelled" and item["call_id"] in calls:
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

    def _forget_events(self, gem_id: ObjectId) -> None:
        """A deleted gem's events go with its log: none is told."""
        kept = self._kept.get(str(gem_id))
        if kept is not None:
            kept.events.take()
            kept.events.stale = False


def numbered(found: Sequence[Remembered]) -> list[Numbered]:
    """Stored entries, with what decides how each is sent."""
    return [Numbered(e.seq, e.data.item, e.data.mark, e.data.delivers) for e in found]


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
