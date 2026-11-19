"""
Prices: what a model call cost, worked out here.

No provider says what a request cost. Each reports the tokens it charged for, and the
price of a token is public. So the prices are kept here, by hand, and a cost is the
tokens times the price.

Every price is in US dollars for a million tokens, as each provider's public price list
had it on the date in `PRICED`. A model with no price here, or a request no price here
covers, has no cost: it is shown as unknown, never as nothing.

What a cost leaves out: tax, discounts, and anything a provider charges for beyond
tokens. A request made through the ChatGPT proxy is charged to that plan, not by the
token: its cost here is what the same request would have cost with the API key.

To change a price, change it here and move `PRICED` on. A cost already worked out keeps
the date it was worked out with.
"""

from dataclasses import dataclass
from decimal import Decimal

from app.entities.model import ModelName, Usage

__all__ = ["PRICED", "cost"]

# The day these prices were read.
PRICED = "2026-11-18"

_OPENAI = "https://developers.openai.com/api/docs/pricing"
_ANTHROPIC = "https://platform.claude.com/docs/en/about-claude/pricing"


@dataclass(frozen=True, slots=True)
class _Price:
    """One model's prices, each for a million tokens."""

    input: Decimal
    cache_read: Decimal
    # None if no price is known for writing to a cache: a request that wrote to one
    # then has no cost.
    cache_write: Decimal | None
    output: Decimal
    # Above this many tokens sent, the provider charges the whole request at another
    # rate, which is not kept here: such a request has no cost.
    up_to: int
    source: str


def _price(
    input_: str, cache_read: str, cache_write: str | None, output: str, up_to: int, source: str
) -> _Price:
    return _Price(
        Decimal(input_),
        Decimal(cache_read),
        None if cache_write is None else Decimal(cache_write),
        Decimal(output),
        up_to,
        source,
    )


# A model that is not here has no price yet: "6.1 Terra", "Sonnet 5.5" and "Opus 5.5".
_PRICES: dict[ModelName, _Price] = {
    "6 Luna": _price("0.10", "0.01", "0.125", "0.50", 272_000, _OPENAI),
    "6.1 Sol": _price("2.00", "0.10", "2.50", "10.00", 272_000, _OPENAI),
    # No price for writing to a cache has been read for this model yet.
    "Haiku 5.5": _price("0.10", "0.01", None, "0.50", 100_000, _ANTHROPIC),
}

_MILLION = Decimal(1_000_000)


def cost(model: ModelName, usage: Usage | None) -> Decimal | None:
    """What a model call cost in US dollars, or None if it cannot be worked out.

    None for a call with no usage reported, a model with no price, a request longer
    than the prices cover, and a request that wrote to a cache on a model with no price
    for that.
    """
    price = _PRICES.get(model)
    if usage is None or price is None:
        return None
    if usage.input + usage.cache_read + usage.cache_write > price.up_to:
        return None
    if usage.cache_write and price.cache_write is None:
        return None
    total = (
        usage.input * price.input
        + usage.cache_read * price.cache_read
        + usage.cache_write * (price.cache_write or Decimal(0))
        # Thinking is part of the output, and charged as output.
        + usage.output * price.output
    )
    return total / _MILLION
