"""
Compaction: what a gem's conversation is sent once it is summed up.

A gem has one conversation, and it only grows. When the user asks, everything the gem
remembers so far can be replaced, in what the model is sent, by a summary the gem's own
model writes. The log is never changed: a compaction is an entry of its own
(`context.compacted`), with the summary and `through`: the number of the compaction's own
request (its `request.sent`). Everything the gem remembered before that request was sent
in it, and is summed up. The latest compaction since the gem was last reset is in force.

What is sent, with a compaction in force (`view`): the summary, as one line of ours,
then every remembered entry numbered `through` or after, with the latest cold start's
expiry still applied (see `cold_start.py`). That is what came after the compaction (the
next line that gives the time, the user's next message, and on), and anything stored
while the summary was written (a late result, an event), which the summary does not
hold. Like the expiry, it is worked out from what is stored and the lines in force
alone, so every request after a compaction starts with the same bytes as the one before
it. Nothing from before `through` is kept whole, not even a search: the tools stay
declared as they were, and a deferred tool is loaded again by the next search.

If nothing is remembered since the compaction in force, or since the gem was last reset,
there is nothing to compact (`anything_new`).

The size of a conversation is an estimate (`size`): the characters of what its next
request would be sent (the instructions, the items' text and the tools it is told of in
full, as JSON), divided by four. No tokenizer is used. Where the gem's latest request
says more, the estimate starts from what it was billed (see `compaction_ent.py`).

The summary request is the gem's own model, sent the same instructions, tools and items
as its next request would be, so a warm cache is read, then one line of ours that asks
for the summary (`ASK`), with tool use off and room to write it (`SUMMARY_TOKENS`). After
what the model writes, the code adds a section of its own: identifiers copied exactly
from what it replaces (`exact`).
"""

import json
import re
from collections.abc import Sequence
from typing import NamedTuple

from app.entities.cold_start import Numbered, as_sent
from app.entities.model import Item, Message, ToolSpec

__all__ = [
    "ASK",
    "COMPACT_OVER",
    "HEADINGS",
    "SUMMARY_TOKENS",
    "Compacted",
    "anything_new",
    "asked",
    "exact",
    "size",
    "summed_up",
    "view",
]

# Past this many estimated tokens, a gem's conversation is to be compacted.
COMPACT_OVER = 100_000

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

    An entry with no number came after every request so far, so it is always sent.
    """
    if compacted is None:
        return as_sent(entries, before)
    kept = [entry for entry in entries if entry.seq is None or entry.seq >= compacted.through]
    return [summed_up(compacted.summary), *as_sent(kept, before)]


def anything_new(entries: Sequence[Numbered], compacted: Compacted | None) -> bool:
    """Whether the gem remembers anything the compaction in force does not sum up.

    `entries` are what it remembers since it was last reset.
    """
    floor = 0 if compacted is None else compacted.through
    return any(entry.seq is None or entry.seq >= floor for entry in entries)


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


def exact(summary: str, replaced: Sequence[Item]) -> str:
    """The summary, with the identifiers in what it replaces copied after it.

    Found by pattern in `replaced` (the items it stands for, as the summary request was
    sent them, the summary in force among them), each once, the latest `_EXACT_MAX` of
    them.
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
