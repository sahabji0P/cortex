"""What a model call cost, from the tokens its provider charged for."""

from decimal import Decimal

from app.entities.model import Usage
from app.entities.prices import cost


def test_a_cost_is_each_kind_of_token_times_its_price() -> None:
    usage = Usage(input=300, cache_read=3100, cache_write=0, output=85, thinking=20)

    # 300 at $0.10, 3,100 at $0.01 and 85 at $0.50, each for a million.
    assert cost("6 Luna", usage) == Decimal("0.0001035")


def test_thinking_is_part_of_the_output_and_not_charged_twice() -> None:
    quiet = Usage(input=0, cache_read=0, cache_write=0, output=100)
    thought = Usage(input=0, cache_read=0, cache_write=0, output=100, thinking=60)

    assert cost("6 Luna", thought) == cost("6 Luna", quiet)


def test_tokens_written_to_a_cache_have_their_own_price() -> None:
    usage = Usage(input=0, cache_read=0, cache_write=100_000, output=0)

    assert cost("6 Luna", usage) == Decimal("0.0125")


def test_a_call_with_no_usage_has_no_cost() -> None:
    assert cost("6 Luna", None) is None


def test_a_model_with_no_price_has_no_cost() -> None:
    assert cost("Opus 5.5", Usage(input=10, cache_read=0, cache_write=0, output=10)) is None


def test_a_request_longer_than_the_prices_cover_has_no_cost() -> None:
    long = Usage(input=200_000, cache_read=72_001, cache_write=0, output=10)

    assert cost("6 Luna", long) is None


def test_writing_to_a_cache_with_no_price_for_it_has_no_cost() -> None:
    wrote = Usage(input=10, cache_read=0, cache_write=40, output=10)
    read = Usage(input=10, cache_read=40, cache_write=0, output=10)

    assert cost("Haiku 5.5", wrote) is None
    assert cost("Haiku 5.5", read) == Decimal("0.0000064")
