"""
Compaction: what a gem's conversation is sent once its older part is summed up.

A gem has one conversation, and it only grows. Past a size (`COMPACT_OVER`), the older
part can be replaced, in what the model is sent, by a summary the gem's own model
writes. The log is never changed: a compaction is an entry of its own
(`context.compacted`), with the summary and `through`, the number of the first entry that
is still sent whole. The latest one since the gem was last reset is in force.

What is sent, with a compaction in force (`view`): the summary, as one line of ours,
then every search made before `through` (`tool_search`, its call and its result: what it
loaded stays loaded, so a deferred tool it loaded can still be called), then every
remembered entry numbered `through` or after, with the latest cold start's expiry still
applied (see `cold_start.py`). Like the expiry, it is worked out from what is
stored and the lines in force alone, so every request of a sitting starts with the same
bytes as the one before it.

Where `through` falls (`through`):

- at the start of the latest sitting, which begins after the latest cold start since the
  gem was last reset, or at the conversation's start;
- moved on, if what it keeps whole is over `KEEP_WHOLE` estimated tokens, until it is
  not, keeping the newest; never past the latest place it can fall;
- never before the compaction in force: what that one summed up stays summed up;
- only ever at the start of a turn: before what the user said or an event the gem was
  told (a `user.said` or `event.received`), and before the lines of ours that go with
  it, just before it (the line that gives the time, the one that says a call started).
  Every tool call before it is answered before it, so a call is never parted from its
  result.

If nothing remembered lies between the compaction in force (or the conversation's
start) and `through`, there is nothing to compact.

The size of a conversation is an estimate (`size`): the characters of what its next
request would be sent (the instructions, the items' text and the tools it is told of in
full, as JSON), divided by four. No tokenizer is used. Where the gem's latest request
says more, the estimate starts from what it was billed (see `compaction_ent.py`).

The summary request is the gem's own model, sent the same instructions, tools and items
as its next request would be, so a warm cache is read, then one line of ours that asks
for the summary (`ASK`), with tool use off and room to write it (`SUMMARY_TOKENS`). After
what the model writes, the code adds a section of its own: identifiers copied exactly
from the part being replaced (`exact`).
"""

import json
import re
from collections.abc import Sequence
from typing import NamedTuple

from app.entities.cold_start import Numbered, as_sent
from app.entities.model import SEARCH_TOOL, Item, Message, ToolSpec

__all__ = [
    "ASK",
    "COMPACT_OVER",
    "HEADINGS",
    "KEEP_WHOLE",
    "SUMMARY_TOKENS",
    "Compacted",
    "asked",
    "exact",
    "replaced",
    "size",
    "summed_up",
    "through",
    "view",
]

# Past this many estimated tokens, a gem's conversation is to be compacted.
COMPACT_OVER = 100_000

# At most about this many estimated tokens of the latest sitting are kept whole.
KEEP_WHOLE = 20_000

# The most the summary request may write, thinking included, where a provider needs a
# ceiling (Anthropic's). A reply's is far lower (see `model.py`): a summary that ran out
# of it would fail. Every model a gem can use allows more.
SUMMARY_TOKENS = 32_000

# The headings the summary is written under, in order.
HEADINGS: tuple[str, ...] = (
    "What the user wants",
    "Preferences",
    "Decisions",
    "Done",
    "Open",
    "Exact details (names, numbers, dates)",
    "Unverified",
)

# The line of ours that asks for the summary, after everything the gem is sent.
# THE OWNER HAS NOT RULED ON THIS WORDING.
ASK = (
    "The conversation so far is about to be shortened. Write a summary to stand in for "
    "its older part. The latest part of the conversation will follow your summary word "
    "for word, so give the older part its weight: keep what it holds that the latest "
    "part does not. Write only the summary, under these headings, in this order:\n"
    + "\n".join(f"- {heading}" for heading in HEADINGS)
    + "\nKeep names, numbers, dates, amounts and promises exactly as they were. Under "
    "Unverified, put what was said or assumed and never checked. Under a heading with "
    "nothing to say, write 'None'."
)

# What the summary is sent under, in the line of ours that carries it.
_SUMMED_UP = "The conversation before this point, summed up:\n\n"

# The section the code adds after the summary, and how much it holds at most.
_EXACT = "Copied exactly from the earlier conversation:"
_EXACT_MAX = 50

# What is copied exactly: addresses, e-mail addresses, amounts of money, and ids (a run
# of letters and digits with both in it, at least six long).
_IDENTIFIERS = re.compile(
    r"https?://[^\s<>\"')\]]*[^\s<>\"')\].,;:!?]"
    r"|[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
    r"|(?:[$€£₹]|\b(?:USD|EUR|GBP|INR|Rs\.?)\s?)\d[\d,]*(?:\.\d+)?"
    r"|\b\d[\d,]*(?:\.\d+)?\s?(?:USD|EUR|GBP|INR|dollars|euros|rupees)\b"
    r"|\b(?=[\w-]*\d)(?=[\w-]*[A-Za-z])[A-Za-z0-9][\w-]{5,}\b"
)


class Compacted(NamedTuple):
    """The compaction in force: the summary, and the first entry still sent whole."""

    summary: str
    through: int


def asked() -> Item:
    """The line of ours that asks for the summary."""
    return Message(role="developer", content=ASK)


def summed_up(summary: str) -> Item:
    """The line of ours that carries the summary, in the shape our other lines take."""
    return Message(role="developer", content=_SUMMED_UP + summary)


def view(
    entries: Sequence[Numbered], before: int | None, compacted: Compacted | None
) -> list[Item]:
    """The items as the model is sent them: summed up before `through`, expired before `before`.

    The searches before `through` stay, after the summary. An entry with no number came
    after every request so far, so it is always sent.
    """
    if compacted is None:
        return as_sent(entries, before)
    searches = _searches(entries, compacted.through)
    kept = [
        entry
        for entry in entries
        if entry.seq is None or entry.seq >= compacted.through or _call_id(entry) in searches
    ]
    return [summed_up(compacted.summary), *as_sent(kept, before)]


def replaced(
    entries: Sequence[Numbered], before: int | None, compacted: Compacted | None, line: int
) -> list[Item]:
    """What a compaction drawn at `line` stands for, as the model was sent it.

    The summary in force, then every entry from its line up to `line`, but the searches:
    they are still sent after the new summary.
    """
    floor = 0 if compacted is None else compacted.through
    searches = _searches(entries, line)
    sent = as_sent(entries, before)
    return [
        *([] if compacted is None else [summed_up(compacted.summary)]),
        *(
            item
            for entry, item in zip(entries, sent, strict=True)
            if entry.seq is not None
            and floor <= entry.seq < line
            and _call_id(entry) not in searches
        ),
    ]


def _searches(entries: Sequence[Numbered], line: int) -> set[str]:
    """The call ids of the searches made before `line`."""
    return {
        entry.item["call_id"]
        for entry in entries
        if entry.seq is not None
        and entry.seq < line
        and "type" in entry.item
        and entry.item["type"] == "function_call"
        and entry.item["name"] == SEARCH_TOOL
    }


def _call_id(entry: Numbered) -> str | None:
    """The call an entry is or answers, if it is a call or a call's result."""
    return entry.item["call_id"] if "type" in entry.item else None


def _text(item: Item) -> str:
    """What of an item is text the model reads."""
    if "role" in item:
        return item["content"]
    if item["type"] == "function_call":
        return item["name"] + item["arguments"]
    return item["output"]


def size(instructions: str, items: Sequence[Item], tools: Sequence[ToolSpec] = ()) -> int:
    """The estimated tokens of a request sent these instructions, items and tools.

    A tool counts as its name, description and parameters, as JSON. A deferred one is not
    counted: it is not in what the model reads until a search loads it.
    """
    told = sum(
        len(json.dumps({"name": t.name, "description": t.description, "parameters": t.parameters}))
        for t in tools
        if not t.deferred
    )
    return (len(instructions) + sum(len(_text(item)) for item in items) + told) // 4


def through(
    entries: Sequence[Numbered],
    *,
    cold: int | None,
    before: int | None,
    compacted: Compacted | None,
) -> int | None:
    """Where a compaction now would draw its line, or None if there is nothing to compact.

    `entries` are what the gem remembers since it was last reset, `cold` the number of
    the latest cold start's own entry since then, `before` the line of the expiry that
    applies now, and `compacted` the compaction in force.
    """
    floor = 0 if compacted is None else compacted.through
    start = max(floor, 0 if cold is None else cold + 1)
    sent = as_sent(entries, before)
    places: list[int] = []
    unanswered: set[str] = set()
    # Where the lines of ours just before this entry begin, if it follows any.
    lines: int | None = None
    for index, entry in enumerate(entries):
        item = entry.item
        if "role" in item and item["role"] == "developer":
            lines = index if lines is None else lines
            continue
        place = index if lines is None else lines
        lines = None
        if "role" in item:
            seq = entries[place].seq
            if item["role"] == "user" and not unanswered and seq is not None and seq >= start:
                places.append(place)
        elif item["type"] == "function_call":
            unanswered.add(item["call_id"])
        else:
            unanswered.discard(item["call_id"])
    if not places:
        return None
    # The earliest place whose whole part is small enough; the latest if none is.
    chosen = next((index for index in places if size("", sent[index:]) <= KEEP_WHOLE), places[-1])
    line = entries[chosen].seq
    assert line is not None
    if not any(entry.seq is not None and floor <= entry.seq < line for entry in entries):
        return None
    return line


def exact(summary: str, replaced: Sequence[Item]) -> str:
    """The summary, with the identifiers in the part it replaces copied after it.

    Found by pattern in `replaced` (the items it stands for, as they were sent, the
    summary before it among them), each once, the latest `_EXACT_MAX` of them.
    """
    found: dict[str, None] = {}
    for item in replaced:
        for match in _IDENTIFIERS.findall(_text(item)):
            found.pop(match, None)
            found[match] = None
    copied = list(found)[-_EXACT_MAX:]
    summary = summary.strip()
    if not copied:
        return summary
    return f"{summary}\n\n{_EXACT}\n" + "\n".join(f"- {one}" for one in copied)
