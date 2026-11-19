"""
What joins a gem's history, by the same rules in a call and in writing.

The user's turn stays open until a reply to it leaves something in the history: some of
its text was delivered, or it called a tool. What the user says before then joins the
same turn, on a new line. In a call, speech recognition often splits one sentence in
two, and the second half is not a new request. In writing, a message sent before the
reply to the one before it got anywhere is the same: the user is still saying what they
want. A reply cut before it left anything leaves the turn open, and the next reply
answers all of it.

A turn joins the history as one user item, and is final from then on. Until then it is
the last thing a request is sent, and the one thing in a request that can change
afterwards: it can gain a line. So it is never stored while it is open.

Late results (see `late_results.py`) close the open turn and follow it (`told`). So a
turn that ends while results wait is answered with them in front of it.

Everything else is added to the history once it is final, and is added with what is
kept beside it (`Added`): when it was added, what a tool call showed, and what a call's
result stands for.

The conversation is one, whether the user calls the gem or writes to it. A line of ours
says when a call starts and when it ends (`mode_line`): before the first turn of every
call, and before the first written turn that follows a call. An empty conversation
counts as written. Nothing is written when a call ends: the line is written when the
next turn needs it, and a call that follows a call is marked by its own opening line.
A line of ours is stored together with the first item that follows it (`settled`), so a
call in which nothing was said stores nothing.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from app.entities.late_results import LateResult
from app.entities.loop import ResultMark, ToolCall
from app.entities.model import Item

__all__ = [
    "Added",
    "Mode",
    "UserTurn",
    "added_reply",
    "line",
    "mode_line",
    "settled",
    "shown",
    "stubs",
    "told",
]

# How the conversation is held: spoken in a call, or written.
type Mode = Literal["call", "chat"]

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
    # When it was added. The user's turn carries when its first piece came.
    at: datetime
    # For a tool call, the text of its `tool.call` event, if it had one.
    shown: str | None = None
    # For a call's result, what it stands for beyond its text (see `ResultMark`).
    mark: ResultMark | None = None


class UserTurn:
    """The user's turn that no reply has got through to yet, as the pieces it came in."""

    def __init__(self) -> None:
        self._pieces: list[str] = []
        self._at: datetime | None = None

    @property
    def open(self) -> bool:
        """Whether the user has said anything that no reply has got through to yet."""
        return bool(self._pieces)

    def add(self, text: str, at: datetime | None = None) -> None:
        """What the user says next joins the turn, on a new line. `at` is when it came."""
        if not self._pieces:
            self._at = at or datetime.now(UTC)
        self._pieces.append(text)

    def items(self) -> list[Item]:
        """The turn as one user item, for a request, or nothing if no turn is open."""
        return [{"role": "user", "content": "\n".join(self._pieces)}] if self._pieces else []

    def added(self) -> list[Added]:
        """The turn as it would join the history now. It stays open."""
        return [Added(item, self._at or datetime.now(UTC)) for item in self.items()]

    def close(self) -> list[Added]:
        """Take the turn out as it stands, to join the history, where it is final.

        Whatever is added next follows it, and what the user says next is a new turn.
        """
        added = self.added()
        self._pieces, self._at = [], None
        return added

    def answered(self, items: Sequence[Item]) -> list[Added]:
        """A reply to the turn leaves `items` in the history. Return what goes before them.

        If the reply left anything, it got through, so the turn it answers is closed and
        goes before it. If it left nothing, the turn stays open.
        """
        return self.close() if items else []


def line(text: str) -> Added:
    """A line of ours: an item that records what happened, in the history's own voice."""
    return Added({"role": "developer", "content": text}, datetime.now(UTC))


def mode_line(mode: Mode, before: Mode) -> list[Added]:
    """The line that goes before a turn in `mode` after a conversation in `before`, if any.

    `before` is the mode of the conversation's last item, or "chat" if it is empty. A
    call always opens with its line, also straight after another call: each call is
    something that happened, and the second line says the first call is over. Writing
    gets its line only after a call.
    """
    return [line(_MODE_LINES[mode])] if mode == "call" or mode != before else []


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


def stubs(calls: Iterable[ToolCall]) -> list[Added]:
    """Stub calls and their results, as they are added. A stub shows nothing."""
    now = datetime.now(UTC)
    return [Added(item, now) for call in calls for item in call.items()]


def told(turn: UserTurn, results: Sequence[LateResult]) -> list[Added]:
    """Late results as they join the history, after the user's open turn, which is closed.

    The open turn has already been sent to the model, so the results follow it. What the
    user says next is a new turn after them: a turn that ends while results wait is
    answered with the results in front of it, in one reply. Nothing if there are none.
    """
    if not results:
        return []
    return [*turn.close(), *stubs(late.stub for late in results)]


def added_reply(items: Sequence[Item], calls: Sequence[ToolCall], at: datetime) -> list[Added]:
    """A reply's items as they are added, with what each tool call showed and each result's mark.

    `calls` are the reply's tool calls, settled: each has its result and its mark.
    """
    by_id = {call.call_id: call for call in calls}
    added: list[Added] = []
    for item in items:
        if "type" not in item:
            added.append(Added(item, at))
        elif item["type"] == "function_call":
            added.append(Added(item, at, shown=shown(by_id[item["call_id"]])))
        else:
            added.append(Added(item, at, mark=by_id[item["call_id"]].mark))
    return added


def shown(call: ToolCall) -> str | None:
    """The text of the `tool.call` event a call sent, worked out again from its arguments."""
    if call.tool is None:
        return None  # a tool the conversation does not have shows nothing
    try:
        return call.tool.shown(call.arguments)
    except Exception:
        return None  # arguments that cannot be read send no event
