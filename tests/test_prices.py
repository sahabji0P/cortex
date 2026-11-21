"""What a model call cost, from the tokens its provider charged for."""

from decimal import Decimal

import pytest

from app.entities import prices
from app.entities.model import Usage
from app.entities.prices import cost


def test_a_cost_is_each_kind_of_token_times_its_price() -> None:
    usage = Usage(input=300, cache_read=3100, cache_write=0, output=85, thinking=20)

    # 300 at $0.10, 3,100 at $0.01 and 85 at $0.50, each for a million.
    assert cost("6 Luna", usage) == Decimal("0.0001035")


def test_every_model_on_offer_has_a_price() -> None:
    usage = Usage(input=1000, cache_read=10_000, cache_write=2000, output=500)

    # Input, cache read, cache write and output, each for a million:
    # 6 Luna $0.10, $0.01, $0.125, $0.50.
    assert cost("6 Luna", usage) == Decimal("0.0007")
    # 6.1 Terra $1.00, $0.05, $1.25, $5.00.
    assert cost("6.1 Terra", usage) == Decimal("0.0065")
    # 6.1 Sol $2.00, $0.10, $2.50, $10.00.
    assert cost("6.1 Sol", usage) == Decimal("0.013")
    # Haiku 5.5 $0.10, $0.01, $0.125, $0.50.
    assert cost("Haiku 5.5", usage) == Decimal("0.0007")
    # Sonnet 5.5 $2, $0.10, $2.50, $10.
    assert cost("Sonnet 5.5", usage) == Decimal("0.013")
    # Opus 5.5 $4, $0.20, $5, $20.
    assert cost("Opus 5.5", usage) == Decimal("0.026")


def test_thinking_is_part_of_the_output_and_not_charged_twice() -> None:
    quiet = Usage(input=0, cache_read=0, cache_write=0, output=100)
    thought = Usage(input=0, cache_read=0, cache_write=0, output=100, thinking=60)

    assert cost("6 Luna", thought) == cost("6 Luna", quiet)


def test_tokens_written_to_a_cache_have_their_own_price() -> None:
    usage = Usage(input=0, cache_read=0, cache_write=100_000, output=0)

    # 100,000 at $0.125 for a million.
    assert cost("6 Luna", usage) == Decimal("0.0125")


def test_a_call_with_no_usage_has_no_cost() -> None:
    assert cost("6 Luna", None) is None


def test_a_model_with_no_price_has_no_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(prices._PRICES, "Opus 5.5")

    assert cost("Opus 5.5", Usage(input=10, cache_read=0, cache_write=0, output=10)) is None


def test_openai_charges_a_whole_request_more_above_272k_tokens_sent() -> None:
    at = Usage(input=200_000, cache_read=71_000, cache_write=1000, output=10)
    over = Usage(input=200_000, cache_read=71_001, cache_write=1000, output=10)

    # 272,000 sent: 200,000 at $0.10, 71,000 at $0.01, 1,000 at $0.125, 10 at $0.50.
    assert cost("6 Luna", at) == Decimal("0.02084")
    # 272,001 sent: 200,000 at $0.20, 71,001 at $0.02, 1,000 at $0.25, 10 at $0.75.
    assert cost("6 Luna", over) == Decimal("0.04167752")


def test_haiku_charges_a_whole_request_more_above_100k_tokens_sent() -> None:
    at = Usage(input=90_000, cache_read=9000, cache_write=1000, output=100)
    over = Usage(input=90_000, cache_read=9001, cache_write=1000, output=100)

    # 100,000 sent: 90,000 at $0.10, 9,000 at $0.01, 1,000 at $0.125, 100 at $0.50.
    assert cost("Haiku 5.5", at) == Decimal("0.009265")
    # 100,001 sent: 90,000 at $0.50, 9,001 at $0.05, 1,000 at $0.625, 100 at $2.50.
    assert cost("Haiku 5.5", over) == Decimal("0.04632505")


def test_sonnet_keeps_one_price_for_a_long_request() -> None:
    usage = Usage(input=900_000, cache_read=0, cache_write=0, output=1000)

    # 900,000 at $2 and 1,000 at $10, each for a million.
    assert cost("Sonnet 5.5", usage) == Decimal("1.81")


def test_a_request_longer_than_the_prices_cover_has_no_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    short_only = prices._price(prices._OPENAI, ("0.10", "0.01", "0.125", "0.50"), 272_000)
    monkeypatch.setitem(prices._PRICES, "6 Luna", short_only)
    long = Usage(input=200_000, cache_read=72_001, cache_write=0, output=10)

    assert cost("6 Luna", long) is None


def test_writing_to_a_cache_with_no_price_for_it_has_no_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    no_write = prices._price(prices._ANTHROPIC, ("0.10", "0.01", None, "0.50"))
    monkeypatch.setitem(prices._PRICES, "Haiku 5.5", no_write)
    wrote = Usage(input=10, cache_read=0, cache_write=40, output=10)
    read = Usage(input=10, cache_read=40, cache_write=0, output=10)

    assert cost("Haiku 5.5", wrote) is None
    # 10 at $0.10, 40 at $0.01 and 10 at $0.50, each for a million.
    assert cost("Haiku 5.5", read) == Decimal("0.0000064")
