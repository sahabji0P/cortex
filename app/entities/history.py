"""
What joins a gem's history, by the same rules in a call and in writing.

Everything is added to the history once it is final, and nothing in the history is
changed afterwards. (At a cold start, old long tool results are sent expired, and stay
stored as they were: see `cold_start.py`.) What the user says is final when they have
said it, so it is added then, as an item of its own (`said`). If no reply has got
through to it when they speak again (the reply was cut before it left anything, or it
failed), what they say next is one more user item after it, and the next reply answers
both. In a call, speech
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
A line of ours is final when it is written, like everything else, so it is stored then:
a call in which nothing was said leaves the line that opened it.

Another line of ours gives the date and the time, in the user's time zone as the app
sends it, or in UTC (`time_line`). It is not in the instructions, which stay the same
from day to day so a prompt cache holds. It goes after the line that opens a call, and
in writing before a message when none has been written yet, the last is more than ten
minutes old, or the date has changed since (`time_due`). A reply with no message gets
none.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from app.entities.late_results import LateResult, delivers
from app.entities.loop import ResultMark, ToolCall
from app.entities.model import Item

__all__ = [
    "Added",
    "Mode",
    "Source",
    "TimeTold",
    "added_reply",
    "line",
    "mode_line",
    "of_turn",
    "said",
    "shown",
    "stubs",
    "time_due",
    "time_line",
    "told",
    "zone_named",
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
# - `background-result`  the stub call we wrote to carry the result of a call that
#                  went on in the background after its reply was over, and that result
# - `lost-call`    the stub that closes a call lost to a restart, and its result
# - `call-line`, `chat-line`  our line that says a call started, or ended
# - `greeting`     our line that asks the gem to speak first in a call
# - `no-words`     our line that says the user made a sound with no words in it
# - `time-line`    our line that gives the date and the time
# - `event`        an event a watch of the gem's caught, told as a user message in an
#                  event tag: outside data, not the user's words
type Source = Literal[
    "user",
    "gem",
    "tool",
    "placeholder",
    "background-result",
    "lost-call",
    "call-line",
    "chat-line",
    "greeting",
    "no-words",
    "time-line",
    "event",
]

# The line of ours that goes before the first turn in each mode, after a turn in the
# other. The gem is told one instruction text however it is reached, and that text says
# a line in the conversation marks when a call starts and when it ends: these are those
# lines.
_MODE_LINES: dict[Mode, str] = {
    "call": "The user has called you. From here on the conversation is spoken.",
    "chat": "The call has ended. From here on the conversation is written.",
}


# A line that gives the time is written again in writing once the last is older than this.
TIME_LINE_GAP = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class TimeTold:
    """What a conversation needs to give the date and the time.

    `zone` is the user's time zone as the app sent it, an IANA name such as
    `Asia/Kolkata`, or None. `last` is when the last line that gave the time was
    written, or None if none was.
    """

    zone: str | None = None
    last: datetime | None = None


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
    # For a stub call of ours and its result: the id of the call whose result they
    # carry. Kept beside them, so nobody has to read it out of the stub's arguments.
    # For an event: the number of the `event.came` it tells, in the gem's log.
    delivers: str | int | None = None


def said(text: str, at: datetime | None = None, turn: int | None = None) -> Added:
    """What the user said, typed or spoken, as it joins the history: one item, final.

    `at` is when it arrived, if not now. `turn` is the number of the turn it opened in
    the gem's log, if it has one.
    """
    return Added({"role": "user", "content": text}, at or datetime.now(UTC), turn=turn)


def line(text: str, source: Source, at: datetime | None = None) -> Added:
    """A line of ours: an item that records what happened, in the history's own voice.

    `source` says which line it is. `at` is when it was written, if not now.
    """
    return Added({"role": "developer", "content": text}, at or datetime.now(UTC), source=source)


def zone_named(name: object) -> ZoneInfo | None:
    """The time zone an IANA name stands for, or None if it is not one that can be read.

    Whatever is sent is taken: a name that cannot be read counts as none sent.
    """
    if not isinstance(name, str) or not name or len(name) > 64:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None  # not a name, or one this machine has no zone for


def time_line(zone: str | None, at: datetime | None = None) -> Added:
    """The line of ours that gives the date and the time at `at` (now if not given).

    In `zone` if it can be read, or else in UTC, saying the user's zone is not known.
    THE OWNER APPROVED THIS WORDING ON 2026-11-20.
    """
    at = at or datetime.now(UTC)
    tz = zone_named(zone)
    if tz is None:
        text = f"It is {_when(at.astimezone(UTC))} UTC. The user's time zone is not known."
    else:
        text = f"It is {_when(at.astimezone(tz))} in {tz.key}."
    return line(text, "time-line", at)


def time_due(time: TimeTold, at: datetime) -> bool:
    """Whether a message sent at `at` needs a line that gives the time before it.

    It does when none has been written, the last is more than ten minutes old, or the
    date has changed since, in the user's time zone.
    """
    if time.last is None:
        return True
    if at - time.last > TIME_LINE_GAP:
        return True
    tz = zone_named(time.zone) or UTC
    return time.last.astimezone(tz).date() != at.astimezone(tz).date()


def _when(at: datetime) -> str:
    """A time as the line says it: "Friday, 20 November 2026, 7:46 pm"."""
    hour = at.hour % 12 or 12
    half = "am" if at.hour < 12 else "pm"
    return f"{at:%A}, {at.day} {at:%B} {at.year}, {hour}:{at.minute:02d} {half}"


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


def stubs(calls: Iterable[ToolCall], source: Source = "background-result") -> list[Added]:
    """Stub calls and their results, as they are added. A stub shows nothing.

    `source` says what they carry: the result of a call that went on in the background,
    or the close of a call that was lost. Each says which call that was.
    """
    now = datetime.now(UTC)
    return [
        Added(item, now, source=source, delivers=delivers(call.items()[0]))
        for call in calls
        for item in call.items()
    ]


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
