"""Where a compaction draws its line, what is sent with one in force, and how big a request is."""

from app.entities.cold_start import EXPIRED, KEEP_UNDER, Numbered
from app.entities.compaction import (
    KEEP_WHOLE,
    Compacted,
    asked,
    exact,
    size,
    summed_up,
    through,
    view,
)
from app.entities.model import Item

_LONG = "x" * KEEP_UNDER


def _user(seq: int | None, text: str = "hi") -> Numbered:
    return Numbered(seq, {"role": "user", "content": text})


def _gem(seq: int | None, text: str = "hello") -> Numbered:
    return Numbered(seq, {"role": "assistant", "content": text})


def _note(seq: int | None, text: str = "a line of ours") -> Numbered:
    return Numbered(seq, {"role": "developer", "content": text})


def _call(seq: int | None, call_id: str) -> Numbered:
    return Numbered(
        seq, {"type": "function_call", "call_id": call_id, "name": "web_search", "arguments": "{}"}
    )


def _result(seq: int | None, call_id: str, output: str = "found") -> Numbered:
    return Numbered(seq, {"type": "function_call_output", "call_id": call_id, "output": output})


def _turns(count: int, *, first: int = 0, text: str = "hi") -> list[Numbered]:
    """`count` turns of a user message and a reply, numbered from `first`, two apart."""
    return [
        entry
        for turn in range(count)
        for entry in (_user(first + 2 * turn, text), _gem(first + 2 * turn + 1))
    ]


# ---- the size


def test_the_size_is_the_characters_of_the_instructions_and_the_items_over_four() -> None:
    items: list[Item] = [
        {"role": "user", "content": "a" * 10},
        {"type": "function_call", "call_id": "c", "name": "abcd", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c", "output": "b" * 20},
    ]

    assert size("i" * 8, items) == (8 + 10 + 6 + 20) // 4
    assert size("", []) == 0


# ---- where the line falls


def test_with_no_cold_start_the_line_is_at_the_start_and_there_is_nothing_before_it() -> None:
    assert through(_turns(3), cold=None, before=None, compacted=None) is None


def test_the_line_is_at_the_start_of_the_latest_sitting() -> None:
    # A cold start written as entry 5: the sitting begins with the turn after it.
    entries = [*_turns(2), *_turns(2, first=6)]

    assert through(entries, cold=5, before=None, compacted=None) == 6


def test_the_line_falls_only_before_what_the_user_said() -> None:
    # The sitting begins with a reply: the first place is the user's.
    entries = [*_turns(2), _gem(5), _user(6), _gem(7)]

    assert through(entries, cold=4, before=None, compacted=None) == 6


def test_the_lines_of_ours_just_before_what_the_user_said_go_with_it() -> None:
    # A line that says a call started and one that gives the time, then what was said.
    entries = [*_turns(2), _gem(5), _note(6), _note(7), _user(8), _gem(9)]

    assert through(entries, cold=4, before=None, compacted=None) == 6
    # A line of ours with a reply after it is no start of a turn.
    entries = [*_turns(2), _note(5), _gem(6), _user(7), _gem(8)]
    assert through(entries, cold=4, before=None, compacted=None) == 7


def test_a_first_turn_with_only_its_lines_before_it_has_nothing_to_compact() -> None:
    entries = [_note(0), *_turns(2, first=1)]

    assert through(entries, cold=None, before=None, compacted=None) is None


def test_the_line_never_parts_a_call_from_its_result() -> None:
    # A user message stored between a call and its result is no place for the line.
    entries = [*_turns(2), _call(4, "a"), _user(5), _result(6, "a"), _gem(7), _user(8), _gem(9)]

    assert through(entries, cold=3, before=None, compacted=None) == 8


def test_an_event_told_is_a_place_too() -> None:
    event = Numbered(5, {"role": "user", "content": "<event>came</event>"}, delivers=2)
    entries = [*_turns(2), _gem(4), event, _gem(6)]

    assert through(entries, cold=4, before=None, compacted=None) == 5


def test_a_long_sitting_is_kept_whole_only_for_its_newest_part() -> None:
    # Each turn is about a thousand tokens: the newest twenty fit.
    entries = _turns(40, text="w" * 4000)

    line = through(entries, cold=None, before=None, compacted=None)

    def kept(since: int) -> list[Item]:
        return [entry.item for entry in entries if entry.seq is not None and entry.seq >= since]

    assert line is not None
    assert line % 2 == 0  # before what the user said
    assert size("", kept(line)) <= KEEP_WHOLE < size("", kept(line - 2))


def test_a_newest_turn_over_the_size_alone_is_still_kept_whole() -> None:
    entries = [*_turns(3), _user(6, "w" * 4 * (KEEP_WHOLE + 10)), _gem(7)]

    assert through(entries, cold=None, before=None, compacted=None) == 6


def test_what_is_expired_counts_as_sent_expired() -> None:
    # The results before the line of the expiry are sent short, so the sitting fits.
    entries = [
        e
        for turn in range(30)
        for e in (
            _user(3 * turn),
            _call(3 * turn + 1, f"c{turn}"),
            _result(3 * turn + 2, f"c{turn}", "r" * 4000),
        )
    ]

    assert through(entries, cold=None, before=1000, compacted=None) is None
    assert through(entries, cold=None, before=None, compacted=None) is not None


def test_the_line_never_goes_back_past_the_compaction_in_force() -> None:
    entries = [*_turns(6)]
    compacted = Compacted("summary", through=6)

    # The sitting began earlier, but what was summed up stays summed up.
    assert through(entries, cold=None, before=None, compacted=compacted) is None
    entries = [*entries, *_turns(2, first=12, text="w" * 4 * KEEP_WHOLE)]
    assert through(entries, cold=None, before=None, compacted=compacted) == 14


# ---- what is sent


def test_with_no_compaction_what_is_sent_is_the_expiry_alone() -> None:
    entries = [_user(0), _call(1, "a"), _result(2, "a", _LONG), _gem(3)]

    assert view(entries, 3, None) == [
        _user(0).item,
        _call(1, "a").item,
        {"type": "function_call_output", "call_id": "a", "output": EXPIRED},
        _gem(3).item,
    ]


def test_the_summary_comes_first_then_the_rest_with_the_expiry_applied() -> None:
    entries = [
        *_turns(2),
        _user(4),
        _call(5, "a"),
        _result(6, "a", _LONG),
        _gem(7),
        _user(None, "added since"),
    ]

    sent = view(entries, 8, Compacted("It went well.", through=4))

    assert sent == [
        summed_up("It went well."),
        _user(4).item,
        _call(5, "a").item,
        {"type": "function_call_output", "call_id": "a", "output": EXPIRED},
        _gem(7).item,
        {"role": "user", "content": "added since"},
    ]
    assert sent[0]["role"] == "developer"
    assert "It went well." in sent[0]["content"]


def test_what_is_sent_only_grows_while_the_lines_stay() -> None:
    compacted = Compacted("It went well.", through=4)
    entries = _turns(4)

    first = view(entries, None, compacted)
    second = view([*entries, *_turns(1, first=8)], None, compacted)

    assert second[: len(first)] == first


def test_the_ask_is_a_line_of_ours() -> None:
    assert asked()["role"] == "developer"
    assert "word for word" in asked()["content"]


# ---- what is copied exactly


def test_identifiers_of_the_part_replaced_are_copied_after_the_summary_once_each() -> None:
    replaced: list[Item] = [
        {"role": "user", "content": "Pay $1,250.50 to INV-2041X, see https://example.com/a?b=1."},
        {"role": "assistant", "content": "Paid INV-2041X. Mail sent to a.b@example.org."},
    ]

    written = exact("  The summary.  ", replaced)

    summary, copied = written.split("\n\n")
    assert summary == "The summary."
    assert copied.splitlines()[1:] == [
        "- $1,250.50",
        "- https://example.com/a?b=1",
        "- INV-2041X",
        "- a.b@example.org",
    ]


def test_with_nothing_to_copy_the_summary_stands_alone() -> None:
    assert exact("The summary.\n", [{"role": "user", "content": "hello there"}]) == "The summary."


def test_only_the_latest_identifiers_are_copied() -> None:
    replaced: list[Item] = [{"role": "user", "content": " ".join(f"id{n:04d}x" for n in range(80))}]

    copied = exact("S.", replaced).splitlines()[3:]

    assert len(copied) == 50
    assert copied[-1] == "- id0079x"
