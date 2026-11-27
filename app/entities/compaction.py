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

What keeping a conversation and compacting it would cost is estimated from its size and
the prices (`costs`, see `prices.py`). Keeping it: its next request writes all of it to
the cache, as it does once the cache is gone. Compacting it: the summary request is
read in full, at the plain input price, and writes a summary of about
`SUMMARY_ESTIMATE` tokens; then the next request writes the much smaller conversation to
the cache, about `AFTER_ESTIMATE` tokens.

The summary request is one plain call of the gem's own model, not a reply. It is told
our instructions (`INSTRUCTIONS`), not the gem's, and sent the items the gem's next
request would be, then one plain user message that asks for the summary (`ASK`). It
may not call a tool, is not cached, and writes JSON held to `SCHEMA`: a list of points
for each heading, with room to write it (`SUMMARY_TOKENS`). What does not fit the schema
fails the compaction (`read`). The code writes the summary from the fields (`written`):
a line of ours, the points under their headings, then two sections of its own, taken
from the part it replaces: identifiers copied exactly, and the user's own words, quoted
exactly.
"""

import json
import re
from collections.abc import Sequence
from typing import NamedTuple, cast

from app.entities.cold_start import Numbered, as_sent
from app.entities.model import Item, Message, ModelName, ToolSpec, Usage
from app.entities.prices import cost

__all__ = [
    "AFTER_ESTIMATE",
    "ASK",
    "COMPACT_OVER",
    "HEADINGS",
    "INSTRUCTIONS",
    "SCHEMA",
    "SUMMARY_ESTIMATE",
    "SUMMARY_TOKENS",
    "Compacted",
    "anything_new",
    "asked",
    "costs",
    "read",
    "size",
    "summed_up",
    "view",
    "written",
]

# Past this many estimated tokens, a gem's conversation is to be compacted.
COMPACT_OVER = 100_000

# The most the summary request may write, thinking included, where a provider needs a
# ceiling (Anthropic's). A reply's is far lower (see `model.py`): a summary that ran out
# of it would fail. Every model a gem can use allows more.
SUMMARY_TOKENS = 32_000

# About how many tokens a summary is, and the request after a compaction: for what a
# compaction is estimated to cost.
SUMMARY_ESTIMATE = 2_000
AFTER_ESTIMATE = 2_000

# What the summary is made of: each field the model writes, as a list of short points,
# the heading it is shown under, and what goes in it. In this order.
_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("user_wants", "What the user wants", "What the user is trying to get done."),
    ("preferences", "Preferences", "How the user wants things done, as they said it."),
    ("decisions", "Decisions", "What was decided."),
    ("done", "Done", "What has been done already, tool calls and what they found included."),
    (
        "open",
        "Open",
        "Where the conversation left off: what was asked last, what is pending, and "
        "what was to happen next.",
    ),
    (
        "exact_details",
        "Exact details (names, numbers, dates)",
        "Names, numbers, dates, amounts, addresses and promises, exactly as they were.",
    ),
    ("unverified", "Unverified", "What was assumed or said and never checked."),
)

HEADINGS: tuple[str, ...] = tuple(heading for _, heading, _ in _FIELDS)

# The JSON the model writes: every field, a list of strings, and nothing else.
SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        key: {"type": "array", "items": {"type": "string"}, "description": description}
        for key, _, description in _FIELDS
    },
    "required": [key for key, _, _ in _FIELDS],
    "additionalProperties": False,
}

# What the model is told, in place of the gem's instructions.
INSTRUCTIONS = """\
You sum up a conversation between a user and an assistant. The conversation is about to \
be replaced by your summary: from then on, the summary is all the assistant has of it. \
Summarise the whole conversation, the latest exchange included, so that it can be \
carried on from the summary alone.

- Keep names, numbers, dates, amounts and promises exactly as they were.
- Under "open", say where the conversation left off: what was asked last, and what was \
to happen next.
- Under "unverified", put what was assumed or said and never checked.
- Not every line is the user's. Some are notes from the system the assistant runs in \
(the date and the time, a call starting or ending, an event that came in), some in \
tags, and some are tool calls and their results. Use what they say, but credit the \
user only with what the user said.
- If the conversation opens with an earlier summary, carry forward what in it still \
matters.
- Write each point short and plain. Leave a field empty when there is nothing for it.
"""

# What asks for the summary: a plain message, after the whole conversation.
ASK = "Summarise the conversation above."

# What the summary is sent under, in the line of ours that carries it. The app knows a
# summary by it.
_SUMMED_UP = "The conversation before this point, summed up:\n\n"

# The summary's own first line.
_FOR_REFERENCE = "It is for reference: do not redo what it lists as done."

# What a heading with nothing under it says.
_NONE = "None"

# The two sections the code adds after the headings, and how much each holds at most:
# identifiers, and characters of the user's words.
_EXACT = "Copied exactly from the earlier conversation:"
_EXACT_MAX = 50
_WORDS = "The user's own words, quoted exactly:"
_WORDS_MAX = 2_000

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
    """The message that asks for the summary."""
    return Message(role="user", content=ASK)


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


def costs(model: ModelName, tokens: int) -> tuple[float | None, float | None]:
    """What keeping a conversation of `tokens` and compacting it would cost, in US dollars.

    Each is None where a price it needs is not known.
    """
    keep = cost(model, Usage(input=0, cache_read=0, cache_write=tokens, output=0))
    summary = cost(model, Usage(input=tokens, cache_read=0, cache_write=0, output=SUMMARY_ESTIMATE))
    after = cost(model, Usage(input=0, cache_read=0, cache_write=AFTER_ESTIMATE, output=0))
    compact = None if summary is None or after is None else summary + after
    return (None if keep is None else float(keep), None if compact is None else float(compact))


def read(text: str) -> dict[str, list[str]]:
    """The fields of what the model wrote. Raises `ValueError` if it does not fit `SCHEMA`."""
    try:
        found = json.loads(text)
    except ValueError as exc:
        raise ValueError("the model wrote no JSON") from exc
    if not isinstance(found, dict):
        raise ValueError("the model wrote no object")
    fields = cast("dict[str, object]", found)
    if set(fields) != {key for key, _, _ in _FIELDS}:
        raise ValueError("the model wrote other fields than the summary's")
    read: dict[str, list[str]] = {}
    for key, value in fields.items():
        if not isinstance(value, list):
            raise ValueError(f"the model wrote no list for {key!r}")
        points = cast("list[object]", value)
        if not all(isinstance(point, str) for point in points):
            raise ValueError(f"the model wrote something other than text in {key!r}")
        read[key] = [point.strip() for point in cast("list[str]", points) if point.strip()]
    if not any(read.values()):
        raise ValueError("the model wrote an empty summary")
    return read


def written(fields: dict[str, list[str]], replaced: Sequence[Item], said: Sequence[str]) -> str:
    """The summary: our line, the fields under their headings, then the code's own sections.

    A heading with nothing under it says so. After the headings come the identifiers in
    what it replaces (`replaced`: the items it stands for, as the summary request was
    sent them), and the user's own words (`said`: what the user said in the part it
    replaces, in order). Each section is left out when it holds nothing.
    """
    parts = [_FOR_REFERENCE]
    for key, heading, _ in _FIELDS:
        points = fields[key]
        parts.append(f"{heading}:\n" + ("\n".join(f"- {p}" for p in points) or _NONE))
    copied = _identifiers(replaced)
    if copied:
        parts.append(f"{_EXACT}\n" + "\n".join(f"- {one}" for one in copied))
    quoted = _quoted(said)
    if quoted:
        parts.append(f"{_WORDS}\n" + "\n".join(quoted))
    return "\n\n".join(parts)


def _identifiers(replaced: Sequence[Item]) -> list[str]:
    """The identifiers in `replaced`, each once, the latest `_EXACT_MAX` of them."""
    found: dict[str, None] = {}
    for item in replaced:
        for match in _IDENTIFIERS.findall(_text(item)):
            found.pop(match, None)
            found[match] = None
    return list(found)[-_EXACT_MAX:]


def _quoted(said: Sequence[str]) -> list[str]:
    """The user's words to quote, each exactly as said, in order, within `_WORDS_MAX`.

    The first message, then as many of the newest as fit, with where some are left out
    marked. A message is never cut: one that does not fit is left out whole, the first
    too.
    """
    room = _WORDS_MAX
    first = bool(said) and len(said[0]) <= room
    if first:
        room -= len(said[0])
    newest: list[str] = []
    for text in reversed(said[1:]):
        if len(text) > room:
            break
        newest.insert(0, text)
        room -= len(text)
    kept = [*said[:1], *newest] if first else newest
    if not kept:
        return []
    lines = [f'"{text}"' for text in newest]
    left_out = len(said) - len(kept)
    if left_out:
        lines.insert(0, f"[{left_out} message{'s' if left_out > 1 else ''} left out here]")
    if first:
        lines.insert(0, f'"{said[0]}"')
    return lines
