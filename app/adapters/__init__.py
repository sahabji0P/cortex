import asyncio

from app.adapters.anthropic_adp import AnthropicAdapterClass
from app.adapters.cartesia_stt_adp import CartesiaSTTAdapterClass
from app.adapters.cartesia_tts_adp import CartesiaTTSAdapterClass
from app.adapters.docdb_adp import DocDBAdapterClass
from app.adapters.http_adp import HttpAdapterClass
from app.adapters.mcp_adp import McpAdapterClass
from app.adapters.openai_adp import OpenAIAdapterClass
from app.adapters.parallel_adp import ParallelAdapterClass
from app.adapters.redis_adp import RedisAdapterClass


class AdapterRegistry:
    async def initialize(self) -> None:
        self.DocDB = DocDBAdapterClass()
        self.Cache = RedisAdapterClass()
        self.HTTP = HttpAdapterClass()
        self.OpenAI = OpenAIAdapterClass()
        self.Anthropic = AnthropicAdapterClass()
        self.CartesiaSTT = CartesiaSTTAdapterClass()
        self.CartesiaTTS = CartesiaTTSAdapterClass()
        self.Parallel = ParallelAdapterClass()
        self.Mcp = McpAdapterClass()
        await asyncio.gather(
            self.DocDB.initialize(),
            self.Cache.initialize(),
            self.HTTP.initialize(),
            self.OpenAI.initialize(),
            self.Anthropic.initialize(),
            self.CartesiaSTT.initialize(),
            self.CartesiaTTS.initialize(),
            self.Parallel.initialize(),
            self.Mcp.initialize(),
        )

    async def close(self) -> None:
        await asyncio.gather(
            self.DocDB.close(),
            self.Cache.close(),
            self.HTTP.close(),
            self.OpenAI.close(),
            self.Anthropic.close(),
            self.CartesiaSTT.close(),
            self.CartesiaTTS.close(),
            self.Parallel.close(),
            self.Mcp.close(),
        )


__all__ = ["AdapterRegistry"]
