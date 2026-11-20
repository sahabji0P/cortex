"""A stand-in for the line of ours that gives the time, in tests not about the time.

The line says the time it was written, so a test that compares what is stored or sent
puts `TIME_LINE` where the line is: it is equal to any such line, and to nothing else.
"""

import re
from typing import Any

__all__ = ["TIME_LINE", "is_time_line"]

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
