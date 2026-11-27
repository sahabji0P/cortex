"""
Cold start: what a gem's conversation is sent once its provider's cache is gone.

A gem has one conversation, and it only grows. Inside a sitting every request starts
with the whole of the one before it, so the provider's prompt cache holds and the start
of each request is read from it. When the cache is certainly gone, everything is sent
at full price anyway. That moment is a cold start, and from it on, old tool results are
sent expired instead of whole.

It is a cold start (`cold`) when a gem has had a request since it was last reset, and
one of these holds, the first that does giving the reason:

- `settings`: the model (or the way to its provider), the instructions or the tools
  changed. Any of them changes the start of the request, so nothing after it is read
  from the cache.
- `compaction`: the conversation was compacted since the last request (a
  `context.compacted` after its `request.sent`). The last request was the compaction's
  own, and what it cached starts with what is now summed up, so no request reads it (see
  `compaction.py`).
- `quiet`: the last one was sent longer ago than its provider keeps a cache
  (`CACHE_LIFETIME`).

It is checked once for each reply in writing, and once when a call starts, never during
a call. A cold start is written down in the gem's log (`cache.cold`), with `before`: the
number of the last request sent. Tool results numbered before it are expired from then
on, until the next cold start draws the line further on (`as_sent`). Anything stored
after that request is sent whole: the model has never been sent it, so no cache could
have held it.

The last request is read from the log's `request.sent` entries, which are written like
everything else that happened: one lost to a failed write makes the next reply see an
older request, and it may start cold while the cache is still warm. That costs one
writing of the cache again; what is sent stays the same from then on.

What is stored is never changed: only what the model is sent. And what is sent is
worked out from what is stored and the line alone, so every request of a sitting starts
with the same bytes as the one before it, and the cache holds again from the cold start.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Literal, NamedTuple

from app.entities.loop import ResultMark
from app.entities.model import SEARCH_TOOL, CallResult, Item, Provider

__all__ = [
    "CACHE_LIFETIME",
    "EXPIRED",
    "KEEP_UNDER",
    "Cache",
    "Numbered",
    "Reason",
    "as_sent",
    "cold",
]

# How long each provider keeps a prompt cache after it was last used. Anthropic's lasts
# five minutes unless a request asks for longer, and ours do not (see `model.py`).
CACHE_LIFETIME: dict[Provider, timedelta] = {
    "Anthropic": timedelta(minutes=5),
    "OpenAI": timedelta(minutes=30),
}

# What an expired result is sent as, in place of its output.
EXPIRED = (
    "[Result expired: no longer kept. If you need it, call the tool again. If it was an "
    "action, it already happened: do not repeat it.]"
)

# A result shorter than this many characters is never expired: it costs little to keep,
# and it is where what an action did is said (an id, "done"). Set it to 0 to expire
# every result.
KEEP_UNDER = 1500

# Why a conversation starts cold.
type Reason = Literal["quiet", "settings", "compaction"]


class Numbered(NamedTuple):
    """One thing a gem remembers, with what decides whether it is sent expired."""

    # Its number in the gem's log, or None for one numbered after every request so far.
    seq: int | None
    item: Item
    # What a call's result stands for beyond its text (see `conversation_store.Kept`).
    mark: ResultMark | None = None
    # For a stub call of ours and its result: the call whose result they carry. For an
    # event: the number of the `event.came` it tells.
    delivers: str | int | None = None


class Cache(NamedTuple):
    """Where a gem's cache stands, since the gem was last reset."""

    # The number of the last request sent, and when it was sent. None if there was none.
    sent: int | None = None
    sent_at: datetime | None = None
    # The line drawn by the latest cold start: results before it are sent expired. None
    # if there was none.
    before: int | None = None
    # The number of the latest compaction's own entry (`context.compacted`). None if
    # there was none.
    compacted: int | None = None


def cold(cache: Cache, provider: Provider, *, changed: bool, now: datetime) -> Reason | None:
    """Why a conversation starts cold now, or None if it does not.

    `provider` is the one the conversation's model belongs to, and `changed` whether the
    model, the instructions or the tools differ from what was last written down. When
    more than one reason holds, `settings` comes first, then `compaction`, then `quiet`.
    """
    if cache.sent is None or cache.sent_at is None:
        return None  # nothing was sent, so nothing could be cached
    if cache.before is not None and cache.before >= cache.sent:
        return None  # nothing was sent since the latest cold start: the line cannot move
    if changed:
        return "settings"
    if cache.compacted is not None and cache.compacted > cache.sent:
        return "compaction"
    if now - cache.sent_at > CACHE_LIFETIME[provider]:
        return "quiet"
    return None


def as_sent(entries: Sequence[Numbered], before: int | None) -> list[Item]:
    """The items as the model is sent them, with the results before `before` expired.

    One item for each entry, in the same order. Only the output of a result is ever
    replaced, so every call keeps a result. Never expired: a search's result (it loads
    deferred tools), a result with a mark or one a stub carries (they say what became of
    a call), and one shorter than `KEEP_UNDER`. Everything else is the same item.
    """
    if before is None:
        return [entry.item for entry in entries]
    searches = {
        entry.item["call_id"]
        for entry in entries
        if "type" in entry.item
        and entry.item["type"] == "function_call"
        and entry.item["name"] == SEARCH_TOOL
    }
    sent: list[Item] = []
    for entry in entries:
        item = entry.item
        if (
            entry.seq is not None
            and entry.seq < before
            and "type" in item
            and item["type"] == "function_call_output"
            and item["call_id"] not in searches
            and entry.mark is None
            and entry.delivers is None
            and len(item["output"]) >= KEEP_UNDER
        ):
            # A new item: the stored one is never changed.
            item = CallResult(type="function_call_output", call_id=item["call_id"], output=EXPIRED)
        sent.append(item)
    return sent
