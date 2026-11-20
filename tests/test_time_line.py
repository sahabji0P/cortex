"""The line of ours that gives the date and the time, and when one is due."""

from datetime import UTC, datetime, timedelta

import pytest

from app.entities.history import TimeTold, time_due, time_line, zone_named

# 7:46 pm in India.
_AT = datetime(2026, 11, 20, 14, 16, tzinfo=UTC)


def test_the_line_gives_the_date_and_time_in_the_users_zone() -> None:
    added = time_line("Asia/Kolkata", _AT)

    assert added.item == {
        "role": "developer",
        "content": "It is Friday, 20 November 2026, 7:46 pm in Asia/Kolkata.",
    }
    assert added.source == "time-line"
    assert added.at == _AT


def test_with_no_zone_the_line_gives_utc_and_says_the_zone_is_not_known() -> None:
    assert time_line(None, _AT).item["content"] == (
        "It is Friday, 20 November 2026, 2:16 pm UTC. The user's time zone is not known."
    )


@pytest.mark.parametrize("name", ["", "Asia", "../etc/passwd", "Not/AZone", "x" * 200, 5, {}])
def test_a_zone_that_cannot_be_read_counts_as_none(name: object) -> None:
    assert zone_named(name) is None
    assert time_line(name, _AT).item["content"].endswith("UTC. The user's time zone is not known.")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("at", "said"),
    [
        (datetime(2026, 11, 5, 0, 0, tzinfo=UTC), "Thursday, 5 November 2026, 12:00 am"),
        (datetime(2026, 11, 5, 12, 5, tzinfo=UTC), "Thursday, 5 November 2026, 12:05 pm"),
        (datetime(2026, 11, 5, 9, 7, tzinfo=UTC), "Thursday, 5 November 2026, 9:07 am"),
    ],
)
def test_the_time_is_said_plainly(at: datetime, said: str) -> None:
    assert time_line("UTC", at).item["content"] == f"It is {said} in UTC."


def test_a_line_is_due_when_none_was_written() -> None:
    assert time_due(TimeTold("Asia/Kolkata"), _AT)


def test_a_line_is_due_once_the_last_is_more_than_ten_minutes_old() -> None:
    zone = "Asia/Kolkata"
    assert not time_due(TimeTold(zone, _AT), _AT + timedelta(minutes=10))
    assert time_due(TimeTold(zone, _AT), _AT + timedelta(minutes=10, seconds=1))


def test_a_line_is_due_when_the_date_changes_in_the_users_zone() -> None:
    # 11:55 pm and 12:02 am in India: seven minutes apart, on two days.
    last = datetime(2026, 11, 20, 18, 25, tzinfo=UTC)
    now = datetime(2026, 11, 20, 18, 32, tzinfo=UTC)

    assert time_due(TimeTold("Asia/Kolkata", last), now)
    # In UTC it is the same day, so no line is due yet.
    assert not time_due(TimeTold(None, last), now)
