"""A cold start: when a conversation starts cold, and what it is sent from then on."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.entities import cold_start
from app.entities.cold_start import EXPIRED, KEEP_UNDER, Cache, Numbered, as_sent, cold
from app.entities.model import Item, Provider, to_input, to_messages

_LONG = "x" * KEEP_UNDER
_SENT_AT = datetime(2026, 11, 26, 9, 0, tzinfo=UTC)


def _call(call_id: str, name: str = "web_search") -> Item:
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": "{}"}


def _result(call_id: str, output: str = _LONG) -> Item:
    return {"type": "function_call_output", "call_id": call_id, "output": output}


def _numbered(*items: Item, first: int = 0) -> list[Numbered]:
    return [Numbered(first + index, item) for index, item in enumerate(items)]


# ---- when it is a cold start


def test_a_gem_with_no_request_since_its_reset_is_never_cold() -> None:
    nothing = Cache()
    later = _SENT_AT + timedelta(days=1)

    assert cold(nothing, "Anthropic", changed=True, now=later) is None
    assert cold(nothing, "OpenAI", changed=False, now=later) is None


@pytest.mark.parametrize(
    ("provider", "lifetime"),
    [("Anthropic", timedelta(minutes=5)), ("OpenAI", timedelta(minutes=30))],
)
def test_it_is_cold_once_the_providers_cache_has_certainly_gone(
    provider: Provider, lifetime: timedelta
) -> None:
    cache = Cache(sent=7, sent_at=_SENT_AT)

    assert cold(cache, provider, changed=False, now=_SENT_AT + lifetime) is None
    after = _SENT_AT + lifetime + timedelta(seconds=1)
    assert cold(cache, provider, changed=False, now=after) == "quiet"


def test_a_change_of_settings_is_a_cold_start_however_soon() -> None:
    cache = Cache(sent=7, sent_at=_SENT_AT)

    assert cold(cache, "Anthropic", changed=True, now=_SENT_AT) == "settings"
    # Both: the change is the reason.
    later = _SENT_AT + timedelta(hours=1)
    assert cold(cache, "Anthropic", changed=True, now=later) == "settings"


# ---- what is sent


def test_with_no_cold_start_everything_is_sent_as_stored() -> None:
    entries = _numbered(_call("a"), _result("a"))

    sent = as_sent(entries, None)

    assert sent == [_call("a"), _result("a")]


def test_only_long_results_before_the_line_are_expired() -> None:
    entries = _numbered(
        {"role": "user", "content": "x" * 5000},  # 0
        _call("a"),  # 1
        _result("a"),  # 2: long, before the line
        _call("b"),  # 3
        _result("b", "y" * (KEEP_UNDER - 1)),  # 4: short
        _call("c"),  # 5
        _result("c"),  # 6: at the line
        {"role": "assistant", "content": "x" * 5000},  # 7
    )

    sent = as_sent(entries, 6)

    assert sent == [
        entries[0].item,
        _call("a"),
        _result("a", EXPIRED),
        _call("b"),
        _result("b", "y" * (KEEP_UNDER - 1)),
        _call("c"),
        _result("c"),
        entries[7].item,
    ]


def test_searches_marks_stubs_and_later_items_are_never_expired() -> None:
    entries = [
        Numbered(0, _call("s", "tool_search")),
        Numbered(1, _result("s", json.dumps({"tools": ["one"], "pad": _LONG}))),
        Numbered(2, _call("p")),
        Numbered(3, _result("p"), mark="placeholder"),
        Numbered(4, _call("k", "cancel_tool_call")),
        Numbered(5, _result("k"), mark="cancelled"),
        Numbered(6, _call("stub"), delivers="p"),
        Numbered(7, _result("stub"), delivers="p"),
        # Added after everything was read: numbered after any request.
        Numbered(None, _call("late")),
        Numbered(None, _result("late")),
    ]

    assert as_sent(entries, 100) == [entry.item for entry in entries]


def test_the_view_is_one_for_one_and_the_same_bytes_every_time() -> None:
    entries = _numbered(_call("a"), _result("a"), _call("b"), _result("b"))

    first, second = as_sent(entries, 2), as_sent(entries, 2)

    assert len(first) == len(entries)
    assert json.dumps(first) == json.dumps(second)
    # What is stored is not changed.
    assert entries[1].item == _result("a")


def test_with_no_guard_every_result_before_the_line_is_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cold_start, "KEEP_UNDER", 0)
    entries = _numbered(_call("a"), _result("a", "ok"))

    assert as_sent(entries, 5) == [_call("a"), _result("a", EXPIRED)]


def test_an_expired_history_is_valid_for_both_providers() -> None:
    entries = _numbered(
        {"role": "user", "content": "look it up"},
        _call("a"),
        _result("a"),
        _call("b"),
        _result("b"),
    )

    sent = as_sent(entries, 3)
    messages = to_messages(sent, {"web_search"})
    sent_openai = to_input(sent, {})

    # The call keeps its result, as one block of text.
    blocks = messages[2]["content"]
    assert isinstance(blocks, list)
    assert blocks[0] == {"type": "tool_result", "tool_use_id": "a", "content": EXPIRED}
    assert [item for item in sent_openai if item.get("type") == "function_call_output"] == [
        _result("a", EXPIRED),
        _result("b"),
    ]
