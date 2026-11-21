"""A stand-in for the line of ours that gives the time, in tests not about the time.

The line says the time it was written, so a test that compares what is stored or sent
puts `TIME_LINE` where the line is: it is equal to any such line, and to nothing else.
A test about the time sets the clock the conversations read (`set_clock`).
"""

import re
import sys
from collections.abc import Callable
from datetime import datetime, tzinfo
from typing import Any

import pytest

__all__ = ["TIME_LINE", "is_time_line", "set_clock"]

_SAYS = re.compile(
    r"It is \w+day, \d{1,2} \w+ \d{4}, \d{1,2}:\d\d [ap]m"
    r"( in [\w/+-]+\.| UTC\. The user's time zone is not known\.)"
)


def is_time_line(text: object) -> bool:
    """Whether a text is what the line that gives the time says."""
    return isinstance(text, str) and _SAYS.fullmatch(text) is not None


class _TimeLine:
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, dict):
            return NotImplemented
        item: dict[str, Any] = other
        return item.keys() == {"role", "content"} and (
            item["role"] == "developer" and is_time_line(item["content"])
        )

    def __hash__(self) -> int:
        return 0

    def __repr__(self) -> str:
        return "TIME_LINE"


# Equal to any line of ours that gives the time, as an item of a history.
TIME_LINE: Any = _TimeLine()


def set_clock(monkeypatch: pytest.MonkeyPatch, at: datetime) -> Callable[[datetime], None]:
    """Have every module of the app read `at` as now. Returns a way to move the clock."""
    now = [at]

    class _Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:  # type: ignore[override]
            return now[0] if tz is None else now[0].astimezone(tz)

    for name, module in list(sys.modules.items()):
        if name.startswith("app.") and getattr(module, "datetime", None) is datetime:
            monkeypatch.setattr(module, "datetime", _Clock)

    def move(to: datetime) -> None:
        now[0] = to

    return move
