"""
What joins a gem's history, by the same rules in a call and in writing.

Everything is added to the history once it is final, and nothing in the history is
changed afterwards. What the user says is final when they have said it, so it is added
then, as an item of its own (`said`). If no reply has got through to it when they speak
again (the reply was cut before it left anything, or it failed), what they say next is
one more user item after it, and the next reply answers both. In a call, speech
recognition often splits one sentence in two: the two halves are then two items in a
row, and the model reads them together.

Late results (see `late_results.py`) are added after everything else, as stub calls
with their results (`told`). When the user's turn ends while results wait, they are
added ahead of what the user said, so one reply answers the user and tells them.

Each item is added with what is kept beside it (`Added`): when it was added, what a
tool call showed, and what a call's result stands for.

The conversation is one, whether the user calls the gem or writes to it. A line of ours
says when a call starts and when it ends (`mode_line`): before the first turn of every
call, and before the first written turn that follows a call. An empty conversation
counts as written. Nothing is written when a call ends: the line is written when the
next turn needs it, and a call that follows a call is marked by its own opening line.
A line of ours is stored together with the first item that follows it (`settled`), so a
call in which nothing was said stores nothing.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal

from app.entities.late_results import LateResult
from app.entities.loop import ResultMark, ToolCall
from app.entities.model import Item

__all__ = [
    "Added",
    "Mode",
    "Source",
    "added_reply",
    "line",
    "mode_line",
    "of_turn",
    "said",
    "settled",
    "shown",
    "stubs",
    "told",
]

# How the conversation is held: spoken in a call, or written.
type Mode = Literal["call", "chat"]

# Who wrote an item of the history. The model is not sent this: it is kept beside the
# item, so that what is stored can be told apart without reading its wording.
#
# - `user`         the user's words, typed or spoken
# - `gem`          the gem's own text, or a tool call it made
# - `tool`         a tool's result
# - `placeholder`  the result we gave a call left running when its reply was cut
# - `late-result`  the stub call we wrote to carry a late result, and that result
# - `lost-call`    the stub that closes a call lost to a restart, and its result
# - `call-line`, `chat-line`  our line that says a call started, or ended
# - `greeting`     our line that asks the gem to speak first in a call
# - `no-words`     our line that says the user made a sound with no words in it
type Source = Literal[
    "user",
    "gem",
    "tool",
    "placeholder",
    "late-result",
    "lost-call",
    "call-line",
    "chat-line",
    "greeting",
    "no-words",
]

# The line of ours that goes before the first turn in each mode, after a turn in the
# other. The gem is told one instruction text however it is reached, and that text says
# a line in the conversation marks when a call starts and when it ends: these are those
# lines.
_MODE_LINES: dict[Mode, str] = {
    "call": "The user has called you. From here on the conversation is spoken.",
    "chat": "The call has ended. From here on the conversation is written.",
}


@dataclass(frozen=True, slots=True)
class Added:
    """One item added to a history, with what is kept beside it."""

    item: Item
    # When it was added. What the user said carries when it arrived.
    at: datetime
    # For a tool call, the text of its `tool.call` event, if it had one.
    shown: str | None = None
    # For a call's result, what it stands for beyond its text (see `ResultMark`).
    mark: ResultMark | None = None
    # Who wrote it (see `Source`).
    source: Source = "user"
    # The number of the turn whose reply it was settled with, in the gem's log, if it
    # was settled with one (see `gem_log.py`). Never sent to a model.
    turn: int | None = None


def said(text: str, at: datetime | None = None, turn: int | None = None) -> Added:
    """What the user said, typed or spoken, as it joins the history: one item, final.

    `at` is when it arrived, if not now. `turn` is the number of the turn it opened in
    the gem's log, if it has one.
    """
    return Added({"role": "user", "content": text}, at or datetime.now(UTC), turn=turn)


def line(text: str, source: Source) -> Added:
    """A line of ours: an item that records what happened, in the history's own voice.

    `source` says which line it is.
    """
    return Added({"role": "developer", "content": text}, datetime.now(UTC), source=source)


def mode_line(mode: Mode, before: Mode) -> list[Added]:
    """The line that goes before a turn in `mode` after a conversation in `before`, if any.

    `before` is the mode of the conversation's last item, or "chat" if it is empty. A
    call always opens with its line, also straight after another call: each call is
    something that happened, and the second line says the first call is over. Writing
    gets its line only after a call.
    """
    if mode != "call" and mode == before:
        return []
    return [line(_MODE_LINES[mode], "call-line" if mode == "call" else "chat-line")]


def of_turn(added: Iterable[Added], turn: int | None) -> list[Added]:
    """The same items, each saying the turn whose reply it was settled with."""
    return [replace(one, turn=turn) for one in added]


def settled(added: Sequence[Added]) -> int:
    """How many of `added`, from the first, may be stored now.

    All of them up to the last that is not a line of ours: a line is stored together
    with the first item that follows it, and not at all if nothing does.
    """
    for count in range(len(added), 0, -1):
        item = added[count - 1].item
        if "role" not in item or item["role"] != "developer":
            return count
    return 0


def stubs(calls: Iterable[ToolCall], source: Source = "late-result") -> list[Added]:
    """Stub calls and their results, as they are added. A stub shows nothing.

    `source` says what they carry: a late result, or the close of a call that was lost.
    """
    now = datetime.now(UTC)
    return [Added(item, now, source=source) for call in calls for item in call.items()]


def told(results: Sequence[LateResult]) -> list[Added]:
    """Late results as they join the history: a stub call and its result for each.

    They follow everything the model has been sent. What the user says next comes after
    them: a turn that ends while results wait is answered with the results in front of
    it, in one reply. Nothing if there are none.
    """
    return stubs(late.stub for late in results)


def added_reply(items: Sequence[Item], calls: Sequence[ToolCall], at: datetime) -> list[Added]:
    """A reply's items as they are added, with what each tool call showed and each result's mark.

    `calls` are the reply's tool calls, settled: each has its result and its mark.
    """
    by_id = {call.call_id: call for call in calls}
    added: list[Added] = []
    for item in items:
        if "type" not in item:
            added.append(Added(item, at, source="gem"))
        elif item["type"] == "function_call":
            added.append(Added(item, at, shown=shown(by_id[item["call_id"]]), source="gem"))
        else:
            mark = by_id[item["call_id"]].mark
            source: Source = "placeholder" if mark == "placeholder" else "tool"
            added.append(Added(item, at, mark=mark, source=source))
    return added


def shown(call: ToolCall) -> str | None:
    """The text of the `tool.call` event a call sent, worked out again from its arguments."""
    if call.tool is None:
        return None  # a tool the conversation does not have shows nothing
    try:
        return call.tool.shown(call.arguments)
    except Exception:
        return None  # arguments that cannot be read send no event
