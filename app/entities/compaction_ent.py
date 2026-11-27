"""
Compaction entity: summing up the older part of a gem's conversation, when asked or by
itself before its cache expires.

What a compaction is, where its line falls and what is sent with one in force is in
`compaction.py`. Here is who makes one, and when.

A compaction takes the gem as a reply does, so there is one writer at a time. It reads
the conversation as it is stored and draws its line first, before anything is written
down. Then it builds the gem's next request exactly as a reply in writing would (see
`ChatEntityClass.next_request`), which may start the conversation cold, and sends it with
the line that asks for the summary after it, with tool use off. The request is written
down like any other, in a turn of its own (`turn.opened` with `by: "compaction"`), so its
tokens and cost are in the log. Once the summary is written, the compaction is stored
(`context.compacted`), at once: if it cannot be, the compaction fails and nothing
changes. It never speaks, and never wakes the gem.

The user's compaction (`compact`) is allowed whenever the gem is between turns, warm or
cold, and refused while a call holds it. A reply being written is not cut: the
compaction starts once it is done. A message sent meanwhile waits, and is answered after
it, as with a reset.

The gem's own (`before_expiry`, the gem's setting): after a reply or a call, if the gem's
conversation is over `COMPACT_OVER`, a compaction is set to run `BEFORE_EXPIRY` before its
cache expires, while the summary request still reads the cache. If its cache is gone
when it runs (the model, the instructions or the tools changed meanwhile), it stops
before the model is asked: only the cold start is stored. Anything that takes the
gem first stops it: a message, a call, an event, a late result (see
`GemConversation.writer`). Once it runs, a message or a call cuts it, as it cuts a reply:
nothing is stored of it. It never runs during a call. A restart loses it, and the user's
compaction is left for when they come back.

Whoever listens is told the gem's key each time what its next request would be sent has
changed: after a reply, a call, a reset or a compaction (`listen`). The state the app
shows is `context`.
"""

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from bson import ObjectId

from app.config import settings
from app.core.errors import ConflictError, NotFoundError, UpstreamError
from app.entities.chat_ent import ChatEntityClass
from app.entities.cold_start import cold
from app.entities.compaction import (
    BEFORE_EXPIRY,
    COMPACT_OVER,
    SUMMARY_TOKENS,
    Compacted,
    Setting,
    asked,
    exact,
    replaced,
    size,
    through,
    view,
)
from app.entities.conversation_ent import (
    ConversationEntityClass,
    Settled,
    Stored,
    gem_key,
    numbered,
)
from app.entities.conversation_store import ConversationStoreClass
from app.entities.darbar_ent import DarbarEntityClass
from app.entities.gem_ent import GemEntityClass
from app.entities.gem_log import TurnRecord
from app.entities.instructions import instruction_layers, joined
from app.entities.model import MODELS, ModelName, RoundDone, TextDelta, Usage, own_instructions

__all__ = ["CompactionEntityClass", "Context", "ContextListener"]

_log = logging.getLogger(__name__)

# Is told the key of a gem whose context has changed.
type ContextListener = Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Context:
    """Where a gem's context stands, for the app.

    `tokens` is the estimated size of its next request. `cache_until` is when its cache
    expires, and `compact_from` when the app is to offer a compaction: `cache_until`,
    if the conversation is over `COMPACT_OVER` and its setting is not `never`. Both are
    None while nothing was sent.
    """

    tokens: int
    cache_until: datetime | None
    compact_from: datetime | None
    compaction: Setting


class _Compacting:
    """A compaction, which holds the gem as a reply does until it is done.

    The user's is not cut: whoever comes next waits for it. The gem's own is, by
    stopping it (`stop`), until its summary is written.
    """

    def __init__(self, stop: Callable[[], object] | None = None) -> None:
        self.over = asyncio.Event()
        self._stop = stop

    def cut(self) -> None:
        if self._stop is not None:
            self._stop()

    def keep(self) -> None:
        """From here on it is not cut: what it wrote is being stored."""
        self._stop = None


class CompactionEntityClass:
    def __init__(
        self,
        *,
        gems: GemEntityClass,
        darbar: DarbarEntityClass,
        store: ConversationStoreClass,
        conversations: ConversationEntityClass,
        chat: ChatEntityClass,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._gems = gems
        self._darbar = darbar
        self._store = store
        self._conversations = conversations
        # What a compaction's request is made with is what the next reply's would be.
        self._chat = chat
        # How a compaction set to run waits for its time. Tests hand in their own.
        self._sleep = sleep
        self._listeners: set[ContextListener] = set()
        # What runs on its own, kept so it is not lost while it runs.
        self._running: set[asyncio.Task[None]] = set()
        conversations.when_settled(self._settled)

    def listen(self, listener: ContextListener) -> None:
        """Tell `listener` each gem whose context changed, until `ignore`."""
        self._listeners.add(listener)

    def ignore(self, listener: ContextListener) -> None:
        self._listeners.discard(listener)

    async def compact(self, gem_id: str) -> Compacted:
        """Compact the gem's conversation now, as the user asks.

        Raises `NotFoundError` for a gem that is not there, `ConflictError` while a call
        holds it or when there is nothing to compact, `UpstreamError` if the summary
        could not be written, and what stopped it if it could not be stored. In each
        case nothing changes.
        """
        if not await self._gems.exists(gem_id):
            raise NotFoundError("Gem")
        key = gem_key(gem_id)
        kept = self._conversations.gem(key)
        if kept.call is not None:
            raise ConflictError("the gem is in a call")
        holding = _Compacting()
        previous, kept.writer = kept.writer, holding
        done = False
        try:
            # A reply being written is not cut: it is stored first.
            if previous is not None:
                await previous.over.wait()
            compacted = await self._compact(key, holding, "user")
            done = True
            return compacted
        finally:
            self._release(key, holding, done)

    async def context(self, gem_id: str) -> Context:
        """Where the gem's context stands. Raises `NotFoundError` if there is no such gem."""
        gem = await self._gems.get(gem_id)
        found = ObjectId(gem_id)
        # What has just been added is on its way to the database: it is waited for.
        await self._conversations.log.written(found)
        name: ModelName = gem.model or settings.voice_model
        cache = await self._store.cache(found)
        before = cache.before
        if cold(cache, _provider(name), changed=False, now=datetime.now(UTC)) is not None:
            before = cache.sent
        items = view(
            numbered(await self._store.load(found)), before, await self._store.compacted(found)
        )
        darbar = (await self._darbar.read()).text
        layers = instruction_layers(darbar=darbar, gem=gem, model_needs=own_instructions(name))
        tokens = size(joined(layers), items)
        cache_until = await self._conversations.cache_until(found, cache)
        offered = tokens > COMPACT_OVER and gem.compaction != "never"
        return Context(
            tokens=tokens,
            cache_until=cache_until,
            compact_from=cache_until if offered else None,
            compaction=gem.compaction,
        )

    async def _compact(
        self, key: str, holding: _Compacting, by: Literal["user", "auto"]
    ) -> Compacted:
        """Write the summary and store the compaction. The gem must be held by `holding`."""
        ready = await self._chat.ready(key)
        if isinstance(ready, str):
            raise UpstreamError(f"the summary could not be written: {ready}")
        gem_id = ready.gem.id
        stored = await self._conversations.load(gem_id)
        # Drawn before anything is written down: a cold start written now would leave
        # the latest sitting empty.
        before = stored.cache.before
        quiet = cold(stored.cache, _provider(ready.name), changed=False, now=datetime.now(UTC))
        if quiet is not None:
            before = stored.cache.sent
        line = through(
            stored.entries,
            cold=await self._sitting(gem_id, stored),
            before=before,
            compacted=stored.compacted,
        )
        if line is None:
            raise ConflictError("nothing to compact")
        made = await self._chat.next_request(ready, key, stored, mode=stored.mode)
        if made.record.started_cold:
            if by == "auto":
                # The settings changed while it waited: the summary would read no cache,
                # and it is for a warm one that it runs. The cold start stays stored.
                raise ConflictError("the cache is cold")
            # The settings changed: it started cold, whatever the time.
            before = stored.cache.sent
        turn = made.record.turn("compaction")
        items = [*made.sent, asked()]
        think = self._conversations.gem(key).late.pending
        request = turn.sent(items, think)
        text, usage = "", cast("Usage | None", None)
        try:
            async for event in made.model.round(
                items,
                instructions=joined(made.layers),
                think=think,
                use_tools=False,
                max_tokens=SUMMARY_TOKENS,
            ):
                if isinstance(event, TextDelta):
                    request.first()
                    text += event.text
                elif isinstance(event, RoundDone):
                    usage = event.usage
        except BaseException as exc:
            request.stopped(text, exc)
            _failed(turn, exc)
            if isinstance(exc, Exception):
                raise UpstreamError(f"the summary could not be written: {exc}") from exc
            raise
        request.done(text, [], usage)
        if not text.strip():
            exc = RuntimeError("the model wrote no summary")
            _failed(turn, exc)
            raise UpstreamError(f"the summary could not be written: {exc}")
        summary = exact(text, replaced(stored.entries, before, stored.compacted, line))
        holding.keep()
        # On its own, so that a write under way is finished even if this is stopped.
        writing = asyncio.ensure_future(
            self._conversations.log.record_compacted(
                gem_id, turn.number, summary=summary, through=line, by=by
            )
        )
        try:
            await asyncio.shield(writing)
        except asyncio.CancelledError:
            # Stopped while it is stored (the backend shuts down): once it is, it is done.
            writing.add_done_callback(functools.partial(self._landed, key, turn))
            raise
        except BaseException as exc:
            _failed(turn, exc)
            raise
        turn.closed(finished=True, kept=None)
        return Compacted(summary, line)

    def _landed(self, key: str, turn: TurnRecord, writing: asyncio.Future[int]) -> None:
        """A compaction stopped while it was stored is done once it is, or failed."""
        error = asyncio.CancelledError() if writing.cancelled() else writing.exception()
        if error is not None:
            _failed(turn, error)
            return
        turn.closed(finished=True, kept=None)
        self._conversations.settled(key, "compaction")

    async def _sitting(self, gem_id: ObjectId, stored: Stored) -> int | None:
        """The number of the cold start the latest sitting begins after, or None.

        A cold start nothing remembered follows began no sitting: it was written by a
        compaction or a reply that then failed. The sitting is the one before it.
        """
        cold_at = stored.cache.cold_at
        while cold_at is not None and not any(
            entry.seq is not None and entry.seq > cold_at for entry in stored.entries
        ):
            cold_at = (await self._store.cache(gem_id, before=cold_at)).cold_at
        return cold_at

    def _release(self, key: str, holding: _Compacting, done: bool) -> None:
        """A compaction is over, done or not: let the gem go."""
        holding.over.set()
        kept = self._conversations.gem(key)
        if kept.writer is holding:
            kept.writer = None
            # What came in while it held the gem is told now.
            self._conversations.wake(key)
        if done:
            self._conversations.settled(key, "compaction")
        self._conversations.let_go(key)

    def _settled(self, key: str, after: Settled) -> None:
        task = asyncio.get_running_loop().create_task(self._after(key, after))
        self._running.add(task)
        task.add_done_callback(self._running.discard)

    async def _after(self, key: str, after: Settled) -> None:
        """Tell whoever listens, and after a reply or a call, set a compaction to run."""
        if not ObjectId.is_valid(key):
            return  # no gem's id: nothing was stored
        # Each on its own, so a slow socket holds up neither the others nor what follows.
        await asyncio.gather(
            *(listener(key) for listener in list(self._listeners)), return_exceptions=True
        )
        if after not in ("reply", "call"):
            return
        try:
            await self._set(key)
        except Exception:
            _log.warning("a compaction of gem %s could not be set to run", key, exc_info=True)

    async def _set(self, key: str) -> None:
        """Set a compaction to run before the cache expires, if the gem is due one."""
        if not await self._gems.exists(key):
            return
        state = await self.context(key)
        if not _due(state):
            return
        assert state.cache_until is not None
        kept = self._conversations.gem(key)
        if kept.writer is not None or kept.call is not None:
            # Taken while it was read: what took it sets one again when it lets it go.
            self._conversations.let_go(key)
            return
        kept.stop_auto()
        kept.auto = asyncio.get_running_loop().create_task(
            self._auto(key, state.cache_until - BEFORE_EXPIRY)
        )

    async def _auto(self, key: str, at: datetime) -> None:
        """Wait until `at`, then compact the gem if nothing has taken it."""
        me = asyncio.current_task()
        try:
            wait = (at - datetime.now(UTC)).total_seconds()
            if wait > 0:
                await self._sleep(wait)
            # Read again: the setting may have changed, or the cache be gone already.
            state = await self.context(key)
            kept = self._conversations.find(key)
            if kept is None or kept.auto is not me:
                return  # stopped while it was read
            kept.auto = None
            if not _due(state) or kept.writer is not None or kept.call is not None:
                self._conversations.let_go(key)
                return
            assert me is not None
            holding = _Compacting(me.cancel)
            kept.writer = holding
        except asyncio.CancelledError:
            return  # stopped: something took the gem
        except Exception:
            _log.warning("a compaction of gem %s could not start", key, exc_info=True)
            kept = self._conversations.find(key)
            if kept is not None and kept.auto is me:
                kept.auto = None
                self._conversations.let_go(key)
            return
        done = False
        try:
            await self._compact(key, holding, "auto")
            done = True
        except asyncio.CancelledError:
            pass  # cut by a message or a call: nothing is stored
        except Exception as exc:
            _log.info("gem %s was not compacted: %s", key, exc)
        finally:
            self._release(key, holding, done)


def _due(state: Context) -> bool:
    """Whether a gem compacts itself now: set to, over the size, and its cache warm."""
    return (
        state.compaction == "before_expiry"
        and state.tokens > COMPACT_OVER
        and state.cache_until is not None
        and datetime.now(UTC) < state.cache_until
    )


def _provider(name: ModelName) -> Literal["OpenAI", "Anthropic"]:
    return next(info.provider for info in MODELS if info.name == name)


def _failed(turn: TurnRecord, error: BaseException) -> None:
    """Close a compaction's turn as failed, or cut if it was stopped."""
    if isinstance(error, Exception):
        turn.failed(error)
    turn.closed(finished=False, kept=None)
