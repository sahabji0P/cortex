"""
Prices: what a model call cost, worked out here.

No provider says what a request cost. Each reports the tokens it charged for, and the
price of a token is public. So the prices are kept here, by hand, and a cost is the
tokens times the price.

Every price is in US dollars for a million tokens. They were read, on the date in
`PRICED`, from each provider's public price page: OpenAI's standard prices at
https://developers.openai.com/api/docs/pricing, and Anthropic's at
https://platform.claude.com/docs/en/about-claude/pricing. A model with no price here,
or a request no price here covers, has no cost: it is shown as unknown, never as
nothing.

Writing to a cache is charged, by both. Anthropic's price is the one for a 5-minute
cache, which is all the backend asks for. OpenAI's is the one for its default 30-minute
cache: its guide to prompt caching, at
https://developers.openai.com/api/docs/guides/prompt-caching, says that from GPT-5.6 on
a write costs 1.25 times the input price, the same as the page's "30m write".

Some models charge a whole request at higher prices once it is long: OpenAI's above
272K input tokens, Anthropic's Haiku 5.5 above 100,000. Every token sent counts towards
that, a cache's included. Sonnet 5.5 and Opus 5.5 keep one price up to their whole
window.

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
PRICED = "2026-11-20"

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
    # Above this many tokens sent, the provider charges the whole request at the prices
    # in `above`; with no `above`, such a request has no cost. None if the model has
    # one price for its whole window.
    up_to: int | None
    source: str
    above: "_Price | None" = None


# Input, cache read, cache write and output, in that order.
_Rates = tuple[str, str, str | None, str]


def _price(
    source: str, rates: _Rates, up_to: int | None = None, above: _Rates | None = None
) -> _Price:
    input_, cache_read, cache_write, output = rates
    return _Price(
        Decimal(input_),
        Decimal(cache_read),
        None if cache_write is None else Decimal(cache_write),
        Decimal(output),
        up_to,
        source,
        None if above is None else _price(source, above),
    )


_PRICES: dict[ModelName, _Price] = {
    "6 Luna": _price(
        _OPENAI, ("0.10", "0.01", "0.125", "0.50"), 272_000, ("0.20", "0.02", "0.25", "0.75")
    ),
    "6.1 Terra": _price(
        _OPENAI, ("1.00", "0.05", "1.25", "5.00"), 272_000, ("2.00", "0.10", "2.50", "7.50")
    ),
    "6.1 Sol": _price(
        _OPENAI, ("2.00", "0.10", "2.50", "10.00"), 272_000, ("4.00", "0.20", "5.00", "15.00")
    ),
    "Haiku 5.5": _price(
        _ANTHROPIC, ("0.10", "0.01", "0.125", "0.50"), 100_000, ("0.50", "0.05", "0.625", "2.50")
    ),
    "Sonnet 5.5": _price(_ANTHROPIC, ("2", "0.10", "2.50", "10")),
    "Opus 5.5": _price(_ANTHROPIC, ("4", "0.20", "5", "20")),
}

_MILLION = Decimal(1_000_000)


def cost(model: ModelName, usage: Usage | None) -> Decimal | None:
    """What a model call cost in US dollars, or None if it cannot be worked out.

    None for a call with no usage reported, a model with no price, a request longer
    than the prices cover, and a request that wrote to a cache on a model with no price
    for that.
    """
    price = _PRICES.get(model)
    if usage is None:
        return None
    sent = usage.input + usage.cache_read + usage.cache_write
    if price is not None and price.up_to is not None and sent > price.up_to:
        price = price.above
    if price is None:
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
