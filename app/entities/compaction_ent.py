"""
Compaction entity: summing up a gem's whole conversation, when the user asks.

What a compaction is and what is sent with one in force is in `compaction.py`. Here is
who makes one, and when.

A compaction takes the gem as a reply does, so there is one writer at a time. It reads
the conversation as it is stored, and is refused if the gem remembers nothing new since
the compaction in force or its last reset. Then it builds the gem's next request exactly
as a reply in writing would (see `ChatEntityClass.next_request`), which may start the
conversation cold, and sends it with the line that asks for the summary after it, with
tool use off. The request is written down like any other, in a turn of its own
(`turn.opened` with `by: "compaction"`), so its tokens and cost are in the log, and its
`request.sent` is the compaction's line (`through`). Once the summary is written, the
compaction is stored (`context.compacted`), at once: if it cannot be, the compaction
fails and nothing changes. It never speaks, and never wakes the gem.

A compaction (`compact`) is only ever made when the user asks. It is allowed whenever
the gem is between turns, warm or cold, and refused while a call holds it. A reply being
written is not cut: the compaction starts once it is done. A message sent meanwhile
waits, and is answered after it, as with a reset. Nothing cuts a compaction: a call
started meanwhile opens once it is done.

Whoever listens is told the gem's key each time what its next request would be sent has
changed: after a reply, a call, a reset or a compaction (`listen`). The state the app
shows is `context`. Its size starts from what the gem's latest request was billed, all
of its input, cached or not, and adds an estimate of what was remembered since
(`_measured`). Where that request says nothing of the next one, the whole of the next
request is estimated (`size`).
"""

import asyncio
import functools
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from bson import ObjectId

from app.config import settings
from app.core.errors import ConflictError, NotFoundError, UpstreamError
from app.entities.chat_ent import ChatEntityClass
from app.entities.cold_start import Numbered, cold
from app.entities.compaction import (
    COMPACT_OVER,
    SUMMARY_TOKENS,
    Compacted,
    anything_new,
    asked,
    exact,
    size,
    view,
)
from app.entities.conversation_ent import (
    ConversationEntityClass,
    Settled,
    gem_key,
    numbered,
)
from app.entities.conversation_store import COLD, COMPACTED, RESET, ConversationStoreClass
from app.entities.darbar_ent import DarbarEntityClass
from app.entities.gem_ent import GemEntityClass
from app.entities.gem_log import TurnRecord
from app.entities.instructions import instruction_layers, joined
from app.entities.model import MODELS, ModelName, RoundDone, TextDelta, Usage, own_instructions

__all__ = ["CompactionEntityClass", "Context", "ContextListener"]

# Is told the key of a gem whose context has changed.
type ContextListener = Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Context:
    """Where a gem's context stands, for the app.

    `tokens` is the estimated size of its next request. `cache_until` is when its cache
    expires, and `compact_from` when the app is to offer a compaction: `cache_until`,
    if the conversation is over `COMPACT_OVER`. Both are
    None while nothing was sent, and after a compaction until the next request: no
    request reads what a compaction's own cached.
    """

    tokens: int
    cache_until: datetime | None
    compact_from: datetime | None


class _Compacting:
    """A compaction, which holds the gem as a reply does until it is done.

    It is never cut: whoever comes next waits for it.
    """

    def __init__(self) -> None:
        self.over = asyncio.Event()

    def cut(self) -> None:
        pass


class CompactionEntityClass:
    def __init__(
        self,
        *,
        gems: GemEntityClass,
        darbar: DarbarEntityClass,
        store: ConversationStoreClass,
        conversations: ConversationEntityClass,
        chat: ChatEntityClass,
    ) -> None:
        self._gems = gems
        self._darbar = darbar
        self._store = store
        self._conversations = conversations
        # What a compaction's request is made with is what the next reply's would be.
        self._chat = chat
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
            compacted = await self._compact(key)
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
        reason = cold(cache, _provider(name), changed=False, now=datetime.now(UTC))
        entries = numbered(await self._store.load(found))
        # A cold start not yet written is not taken off what was billed: the size is then
        # over, never under, while the app offers a compaction.
        tokens = await self._measured(found, entries)
        if tokens is None:
            items = view(
                entries,
                cache.sent if reason is not None else cache.before,
                await self._store.compacted(found),
            )
            darbar = (await self._darbar.read()).text
            layers = instruction_layers(darbar=darbar, gem=gem, model_needs=own_instructions(name))
            key = gem_key(gem_id)
            tools = await self._chat.toolbox(gem, key, [entry.item for entry in entries])
            self._conversations.let_go(key)
            tokens = size(joined(layers), items, tools.specs)
        cache_until = await self._conversations.cache_until(found, cache)
        if reason == "compaction":
            # The last request was a compaction's own: what it cached starts with what
            # is now summed up, and no request sends that again.
            cache_until = None
        return Context(
            tokens=tokens,
            cache_until=cache_until,
            compact_from=cache_until if tokens > COMPACT_OVER else None,
        )

    async def _measured(self, gem_id: ObjectId, entries: Sequence[Numbered]) -> int | None:
        """The size of the gem's next request, from what its latest request was billed.

        All of that request's input, read from a cache or not, and the estimated size of
        what was remembered since it was sent (`entries` are what the gem remembers).
        None if it says nothing of the next one: it was a compaction's own, it reported
        no tokens, or the gem was reset, compacted, started cold or had its model,
        instructions or tools changed since.
        """
        done = await self._store.last(gem_id, "request.done")
        if done is None or done.data.get("usage") is None or done.data.get("request") is None:
            return None
        for kind in (RESET, COMPACTED, COLD, "settings.changed"):
            later = await self._store.last(gem_id, kind)
            if later is not None and later.seq > done.seq:
                return None
        if done.turn is not None:
            opened = await self._store.opened(gem_id, done.turn)
            if opened is not None and opened.data.get("by") == "compaction":
                return None
        sent = await self._store.request_sent(gem_id, done.data["request"], before=done.seq)
        if sent is None:
            return None
        usage = done.data["usage"]
        since = [entry.item for entry in entries if entry.seq is not None and entry.seq > sent.seq]
        return usage["input"] + usage["cache_read"] + usage["cache_write"] + size("", since)

    async def _compact(self, key: str) -> Compacted:
        """Write the summary and store the compaction. The gem must be held."""
        ready = await self._chat.ready(key)
        if isinstance(ready, str):
            raise UpstreamError(f"the summary could not be written: {ready}")
        gem_id = ready.gem.id
        stored = await self._conversations.load(gem_id)
        if not anything_new(stored.entries, stored.compacted):
            raise ConflictError("nothing to compact")
        made = await self._chat.next_request(ready, key, stored, mode=stored.mode)
        turn = made.record.turn("compaction")
        items = [*made.sent, asked()]
        think = self._conversations.gem(key).late.pending
        request = turn.sent(items, think)
        # Everything remembered before the request is in it, and summed up.
        line = request.seq
        if line is None:
            exc = RuntimeError("the summary request could not be written down")
            _failed(turn, exc)
            raise exc
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
        summary = exact(text, made.sent)
        # On its own, so that a write under way is finished even if this is stopped.
        writing = asyncio.ensure_future(
            self._conversations.log.record_compacted(
                gem_id, turn.number, summary=summary, through=line
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
        task = asyncio.get_running_loop().create_task(self._tell(key))
        self._running.add(task)
        task.add_done_callback(self._running.discard)

    async def _tell(self, key: str) -> None:
        """Tell whoever listens that the gem's context changed."""
        if not ObjectId.is_valid(key):
            return  # no gem's id: nothing was stored
        # Each on its own, so a slow socket holds up neither the others nor what follows.
        await asyncio.gather(
            *(listener(key) for listener in list(self._listeners)), return_exceptions=True
        )


def _provider(name: ModelName) -> Literal["OpenAI", "Anthropic"]:
    return next(info.provider for info in MODELS if info.name == name)


def _failed(turn: TurnRecord, error: BaseException) -> None:
    """Close a compaction's turn as failed, or cut if it was stopped."""
    if isinstance(error, Exception):
        turn.failed(error)
    turn.closed(finished=False, kept=None)
