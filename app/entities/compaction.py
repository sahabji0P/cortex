"""
Compaction: what a gem's conversation is sent once its older part is summed up.

A gem has one conversation, and it only grows. Past a size (`COMPACT_OVER`), the older
part can be replaced, in what the model is sent, by a summary the gem's own model
writes. The log is never changed: a compaction is an entry of its own
(`context.compacted`), with the summary and `through`, the number of the first entry that
is still sent whole. The latest one since the gem was last reset is in force.

What is sent, with a compaction in force (`view`): the summary, as one line of ours,
then every remembered entry numbered `through` or after, with the latest cold start's
expiry still applied (see `cold_start.py`). Like the expiry, it is worked out from what is
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

The size of a conversation is an estimate: the characters of what its next request
would be sent (the instructions and the items' text), divided by four (`size`). No
tokenizer is used.

The summary request is the gem's own model, sent the same instructions, tools and items
as its next request would be, so a warm cache is read, then one line of ours that asks
for the summary (`ASK`), with tool use off. After what the model writes, the code adds a
section of its own: identifiers copied exactly from the part being replaced (`exact`).
"""

import re
from collections.abc import Sequence
from datetime import timedelta
from typing import Literal, NamedTuple

from app.entities.cold_start import Numbered, as_sent
from app.entities.model import Item, Message

__all__ = [
    "ASK",
    "BEFORE_EXPIRY",
    "COMPACT_OVER",
    "HEADINGS",
    "KEEP_WHOLE",
    "Compacted",
    "Setting",
    "asked",
    "exact",
    "size",
    "summed_up",
    "through",
    "view",
]

# Past this many estimated tokens, a gem's conversation is to be compacted.
COMPACT_OVER = 100_000

# At most about this many estimated tokens of the latest sitting are kept whole.
KEEP_WHOLE = 20_000

# How long before its cache expires a gem whose setting is `before_expiry` is compacted.
BEFORE_EXPIRY = timedelta(minutes=2)

# When a gem is compacted: by itself, while its cache is still warm, and when the user
# asks (`before_expiry`); only when the user asks (`on_return`); or never.
type Setting = Literal["before_expiry", "on_return", "never"]

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

    An entry with no number came after every request so far, so it is always sent.
    """
    if compacted is None:
        return as_sent(entries, before)
    kept = [entry for entry in entries if entry.seq is None or entry.seq >= compacted.through]
    return [summed_up(compacted.summary), *as_sent(kept, before)]


def _text(item: Item) -> str:
    """What of an item is text the model reads."""
    if "role" in item:
        return item["content"]
    if item["type"] == "function_call":
        return item["name"] + item["arguments"]
    return item["output"]


def size(instructions: str, items: Sequence[Item]) -> int:
    """The estimated tokens of a request sent these instructions and items."""
    return (len(instructions) + sum(len(_text(item)) for item in items)) // 4


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
