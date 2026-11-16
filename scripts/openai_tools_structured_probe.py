"""Try an OpenAI function tool together with a final Pydantic response.

Run from the repository root after setting OPENAI_API_KEY:

    uv run python -m scripts.openai_tools_structured_probe
"""

import asyncio
from typing import Literal

from pydantic import BaseModel, Field

from app.adapters.openai_adp import OpenAIAdapterClass
from app.config import settings


class WeatherLookupArgs(BaseModel):
    """Arguments for looking up current weather in a city."""

    city: str = Field(description="City and country, for example Bengaluru, India")
    unit: Literal["celsius", "fahrenheit"]


class WeatherAnswer(BaseModel):
    """The final user-facing answer after weather data has been retrieved."""

    city: str
    temperature: float
    unit: Literal["celsius", "fahrenheit"]
    summary: str
    used_tool: bool


def lookup_weather(arguments: WeatherLookupArgs) -> dict[str, object]:
    """Return fixed local data so the probe tests OpenAI, not a weather provider."""
    print(f"tool arguments (already Pydantic): {arguments!r}")
    temperature = 24.0 if arguments.unit == "celsius" else 75.2
    return {
        "city": arguments.city,
        "temperature": temperature,
        "unit": arguments.unit,
        "condition": "partly cloudy",
        "source": "local probe fixture",
    }


async def main() -> None:
    if not settings.openai_api_key:
        raise SystemExit("Set OPENAI_API_KEY in .env before running this probe.")

    adapter = OpenAIAdapterClass()
    await adapter.initialize()
    try:
        response = await adapter.structured_response(
            model="Luna",
            input="What is the weather in Bengaluru, India? Use Celsius.",
            instructions=(
                "Use lookup_weather for the data. Then answer using the requested structured "
                "schema. Set used_tool to true."
            ),
            tools=[
                adapter.Tools.pydantic_function(
                    WeatherLookupArgs,
                    name="lookup_weather",
                    description="Look up current weather for a city.",
                )
            ],
            tool_handlers={"lookup_weather": lookup_weather},
            output_model=WeatherAnswer,
            # Force the first turn to demonstrate a function call. Later turns use auto.
            initial_tool_choice={"type": "function", "name": "lookup_weather"},
        )
    finally:
        await adapter.close()

    answer = response.output_parsed
    if answer is None:
        raise RuntimeError("OpenAI returned no parsed WeatherAnswer")

    print("\nfinal Pydantic object:")
    print(answer.model_dump_json(indent=2))
    print("\nusage (includes reasoning-token counts when reported, not hidden thoughts):")
    print(response.usage.model_dump_json(indent=2) if response.usage else "not reported")


if __name__ == "__main__":
    asyncio.run(main())
