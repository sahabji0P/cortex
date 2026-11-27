"""What there is to compact, what is sent with a compaction in force, and how big a request is."""

import json
from typing import Any, cast

import pytest

from app.adapters.openai_adp import OpenAIAdapterClass
from app.entities import prices
from app.entities.cold_start import EXPIRED, KEEP_UNDER, Numbered
from app.entities.compaction import (
    HEADINGS,
    INSTRUCTIONS,
    SCHEMA,
    Compacted,
    anything_new,
    asked,
    costs,
    read,
    size,
    summed_up,
    view,
    written,
)
from app.entities.model import Item, ToolSpec, to_input, to_messages

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


def test_the_size_counts_the_tools_told_in_full_as_json_and_not_the_deferred() -> None:
    told = ToolSpec("find", "Find it.", {"type": "object"})
    deferred = ToolSpec("load", "Load it.", {"type": "object"}, deferred=True)
    schema = json.dumps(
        {"name": "find", "description": "Find it.", "parameters": {"type": "object"}}
    )

    assert size("i" * 8, [], [told, deferred]) == (8 + len(schema)) // 4


# ---- what keeping and compacting would cost


def test_keeping_costs_writing_the_whole_conversation_to_the_cache() -> None:
    keep, _ = costs("Opus 5.5", 50_000)

    # Opus 5.5 writes a cache at $5 a million tokens.
    assert keep == pytest.approx(50_000 * 5 / 1e6)


def test_compacting_costs_the_summary_request_and_the_smaller_request_after_it() -> None:
    _, compact = costs("Opus 5.5", 50_000)

    # The summary request is read in full at $4, and about 2,000 written out at $20; then
    # about 2,000 written to the cache at $5 by the next request. No cache is read.
    assert compact == pytest.approx((50_000 * 4 + 2_000 * 20 + 2_000 * 5) / 1e6)


def test_a_long_conversation_is_priced_as_its_provider_prices_a_long_request() -> None:
    # Haiku 5.5 charges a request of more than 100,000 tokens at higher prices.
    keep, _ = costs("Haiku 5.5", 150_000)

    assert keep == pytest.approx(150_000 * 0.625 / 1e6)


def test_a_cost_whose_price_is_not_known_is_none_never_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(prices._PRICES, "Opus 5.5")
    assert costs("Opus 5.5", 50_000) == (None, None)
    # No price for writing a cache: keeping costs a write, and so does the request after
    # a compaction.
    no_write = prices._price("test", ("2", "0.10", None, "10"))
    monkeypatch.setitem(prices._PRICES, "Sonnet 5.5", no_write)
    assert costs("Sonnet 5.5", 50_000) == (None, None)


# ---- what there is to compact


def test_anything_the_gem_remembers_is_there_to_compact() -> None:
    assert anything_new(_turns(1), None)
    # Even a line of ours alone, or what was added since the last request.
    assert anything_new([_note(0)], None)
    assert anything_new([_user(None)], None)
    assert not anything_new([], None)


def test_only_what_came_after_the_compaction_in_force_is_there_to_compact() -> None:
    compacted = Compacted("summary", through=4)

    assert not anything_new(_turns(2), compacted)
    assert anything_new([*_turns(2), _note(5)], compacted)
    # Something stored while its summary was written is not in it.
    assert anything_new([*_turns(2), _result(4, "late")], compacted)
    assert anything_new([*_turns(2), _user(None)], compacted)


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


def _search(seq: int, call_id: str, loads: str) -> list[Numbered]:
    """A search, and its result that loads `loads`."""
    return [
        Numbered(
            seq,
            {"type": "function_call", "call_id": call_id, "name": "tool_search", "arguments": "{}"},
        ),
        _result(seq + 1, call_id, json.dumps({"tools": [loads]})),
    ]


def _deferred_call(seq: int, call_id: str) -> Numbered:
    return Numbered(
        seq, {"type": "function_call", "call_id": call_id, "name": _DEFERRED, "arguments": "{}"}
    )


_DEFERRED = "server__lookup"


def _loaded_then_called_late() -> list[Numbered]:
    """A deferred tool loaded and called before a compaction at 6, its late result after it."""
    return [
        _user(0),
        *_search(1, "s", _DEFERRED),
        _deferred_call(3, "d"),
        _result(4, "d", "still running"),
        _gem(5),
        # The compaction's request is entry 6; the late result is stored after it.
        _deferred_call(7, "late_d"),
        _result(8, "late_d", "found it"),
        _note(9, "the time"),
        _user(10),
    ]


def test_nothing_before_the_line_is_sent_not_even_a_search() -> None:
    entries = _loaded_then_called_late()

    sent = view(entries, None, Compacted("Looked it up.", through=6))

    assert sent == [summed_up("Looked it up."), *(entry.item for entry in entries[6:])]


def test_a_deferred_tool_called_after_the_summary_with_no_search_is_sent_on_openai() -> None:
    sent = view(_loaded_then_called_late(), None, Compacted("Looked it up.", through=6))
    spec = OpenAIAdapterClass.Tools.function(
        name=_DEFERRED, description="Look up.", parameters={}, strict=False, defer_loading=True
    )

    request = [cast("dict[str, Any]", item) for item in to_input(sent, {_DEFERRED: spec})]

    # No search items: the call goes as it is, as it would in a first request.
    assert [item.get("type") for item in request] == [
        None,
        "function_call",
        "function_call_output",
        None,
        None,
    ]
    assert request[1]["name"] == _DEFERRED


def test_a_deferred_tool_called_after_the_summary_with_no_search_is_sent_on_anthropic() -> None:
    sent = view(_loaded_then_called_late(), None, Compacted("Looked it up.", through=6))

    messages = cast("list[dict[str, Any]]", to_messages(sent, {_DEFERRED, "tool_search"}))

    assert [message["role"] for message in messages] == ["user", "assistant", "user"]
    assert messages[0]["content"][0]["text"].startswith("<note>The conversation before")
    assert messages[1]["content"] == [
        {"type": "tool_use", "id": "late_d", "name": _DEFERRED, "input": {}}
    ]
    # No reference to a loaded tool anywhere: nothing was searched for.
    assert "tool_reference" not in json.dumps(messages)


def test_the_ask_is_a_plain_message_and_the_job_is_in_our_own_instructions() -> None:
    # A plain user message: no note, nothing of ours around it.
    assert asked() == {"role": "user", "content": "Summarise the conversation above."}
    messages = cast("list[dict[str, Any]]", to_messages([asked()]))
    assert messages == [{"role": "user", "content": [{"type": "text", "text": asked()["content"]}]}]
    assert "the latest exchange included" in INSTRUCTIONS
    assert "carried on from the summary alone" in INSTRUCTIONS
    for words in ("exactly", '"open"', '"unverified"'):
        assert words in INSTRUCTIONS


def test_the_schema_has_one_list_for_each_heading_and_nothing_else() -> None:
    properties = cast("dict[str, Any]", SCHEMA["properties"])

    assert SCHEMA["additionalProperties"] is False
    assert SCHEMA["required"] == list(properties)
    assert len(properties) == len(HEADINGS) == 7
    assert all(field["type"] == "array" for field in properties.values())


# ---- what the model wrote

_FIELDS = {
    "user_wants": ["Book a table for four."],
    "preferences": ["Vegetarian."],
    "decisions": [],
    "done": ["Found two places."],
    "open": ["The user is to pick one."],
    "exact_details": ["Friday 4 December, 8 pm"],
    "unverified": [],
}


def test_what_the_model_wrote_is_read_into_its_fields() -> None:
    written_out = json.dumps({**_FIELDS, "done": ["  Found two places. ", " "]})

    assert read(written_out) == _FIELDS


@pytest.mark.parametrize(
    "text",
    [
        "I won't write that summary.",
        "[]",
        json.dumps({**_FIELDS, "extra": []}),
        json.dumps({key: value for key, value in _FIELDS.items() if key != "open"}),
        json.dumps({**_FIELDS, "done": "Found two places."}),
        json.dumps({**_FIELDS, "done": [1]}),
        json.dumps({key: [] for key in _FIELDS}),
    ],
)
def test_what_does_not_fit_the_schema_or_says_nothing_is_not_read(text: str) -> None:
    with pytest.raises(ValueError):
        read(text)


def test_the_summary_is_our_line_then_each_heading_with_none_where_it_is_empty() -> None:
    summary = written(_FIELDS, [{"role": "user", "content": "hello there"}], [])

    assert summary == (
        "It is for reference: do not redo what it lists as done.\n\n"
        "What the user wants:\n- Book a table for four.\n\n"
        "Preferences:\n- Vegetarian.\n\n"
        "Decisions:\nNone\n\n"
        "Done:\n- Found two places.\n\n"
        "Open:\n- The user is to pick one.\n\n"
        "Exact details (names, numbers, dates):\n- Friday 4 December, 8 pm\n\n"
        "Unverified:\nNone"
    )


def test_the_line_it_is_sent_in_starts_as_the_app_knows_it() -> None:
    line = summed_up(written(_FIELDS, [], []))["content"]

    assert line.splitlines()[:3] == [
        "The conversation before this point, summed up:",
        "",
        "It is for reference: do not redo what it lists as done.",
    ]


# ---- what is copied exactly


def test_identifiers_of_the_part_replaced_are_copied_after_the_headings_once_each() -> None:
    replaced: list[Item] = [
        {"role": "user", "content": "Pay $1,250.50 to INV-2041X, see https://example.com/a?b=1."},
        {"role": "assistant", "content": "Paid INV-2041X. Mail sent to a.b@example.org."},
    ]

    copied = written(_FIELDS, replaced, []).split("\n\n")[-1]

    assert copied.splitlines() == [
        "Copied exactly from the earlier conversation:",
        "- $1,250.50",
        "- https://example.com/a?b=1",
        "- INV-2041X",
        "- a.b@example.org",
    ]


def test_only_the_latest_identifiers_are_copied() -> None:
    replaced: list[Item] = [{"role": "user", "content": " ".join(f"id{n:04d}x" for n in range(80))}]

    copied = written(_FIELDS, replaced, []).split("\n\n")[-1].splitlines()[1:]

    assert len(copied) == 50
    assert copied[-1] == "- id0079x"


# ---- the user's own words


def _quoted(said: list[str]) -> list[str]:
    """The section of the user's words, as lines, or none."""
    last = written(_FIELDS, [], said).split("\n\n")[-1].splitlines()
    return last[1:] if last[0] == "The user's own words, quoted exactly:" else []


def test_the_user_s_words_are_quoted_exactly_and_in_order_when_they_fit() -> None:
    said = ["Book a table.", "  For four,\nveg only  ", "Friday."]

    assert _quoted(said) == ['"Book a table."', '"  For four,', 'veg only  "', '"Friday."']


def test_the_first_message_then_the_newest_that_fit_with_the_gap_marked() -> None:
    said = ["first " + "a" * 494, *(f"{n} " + "b" * 498 for n in range(1, 6))]

    quoted = _quoted(said)

    # The first (500), then the newest three (1,500), within 2,000.
    assert quoted == [f'"{said[0]}"', "[2 messages left out here]", *(f'"{t}"' for t in said[3:])]


def test_a_message_is_never_cut_one_that_does_not_fit_is_left_out_whole() -> None:
    long_first = ["x" * 2_001, "the latest"]

    assert _quoted(long_first) == ["[1 message left out here]", '"the latest"']
    # The newest that fit are the latest ones in a row: one too long ends them.
    assert _quoted(["first", "older", "y" * 1_999, "newest"]) == [
        '"first"',
        "[2 messages left out here]",
        '"newest"',
    ]


def test_with_nothing_the_user_said_there_is_no_section() -> None:
    assert _quoted([]) == []
    assert _quoted(["z" * 2_001]) == []
