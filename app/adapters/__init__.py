import asyncio

from app.adapters.anthropic_adp import AnthropicAdapterClass
from app.adapters.docdb_adp import DocDBAdapterClass
from app.adapters.gemini_adp import GeminiAdapterClass
from app.adapters.http_adp import HttpAdapterClass
from app.adapters.openai_adp import OpenAIAdapterClass
from app.adapters.redis_adp import RedisAdapterClass


class AdapterRegistry:
    async def initialize(self) -> None:
        self.DocDB = DocDBAdapterClass()
        self.Cache = RedisAdapterClass()
        self.HTTP = HttpAdapterClass()
        self.Anthropic = AnthropicAdapterClass()
        self.Gemini = GeminiAdapterClass()
        self.OpenAI = OpenAIAdapterClass()
        await asyncio.gather(
            self.DocDB.initialize(),
            self.Cache.initialize(),
            self.HTTP.initialize(),
            self.Anthropic.initialize(),
            self.Gemini.initialize(),
            self.OpenAI.initialize(),
        )

    async def close(self) -> None:
        await asyncio.gather(
            self.DocDB.close(),
            self.Cache.close(),
            self.HTTP.close(),
            self.Anthropic.close(),
            self.Gemini.close(),
            self.OpenAI.close(),
        )


__all__ = ["AdapterRegistry"]
