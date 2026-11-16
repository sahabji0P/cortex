"""
Parallel adapter responsible for web search.

See https://docs.parallel.ai for the Search API.
"""

from typing import Literal

from parallel import AsyncParallel
from parallel.types import SearchResult

from app.config import settings

__all__ = ["ParallelAdapterClass", "SearchResult"]

_TIMEOUT_SECONDS = 20.0


class _Types:
    """Group private Parallel adapter types."""

    # Parallel's speed/depth trade-off, fastest first.
    type SearchMode = Literal["turbo", "fast", "basic", "advanced"]


class ParallelAdapterClass:
    def __init__(self) -> None:
        self._client: AsyncParallel | None = None

    async def initialize(self) -> None:
        if not settings.parallel_api_key:
            return

        self._client = AsyncParallel(
            api_key=settings.parallel_api_key,
            timeout=_TIMEOUT_SECONDS,
            max_retries=0,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()

    @property
    def configured(self) -> bool:
        """Whether an API key was provided, so callers can leave the tool out otherwise."""
        return self._client is not None

    @property
    def client(self) -> AsyncParallel:
        if self._client is None:
            raise RuntimeError("Parallel client not initialized or API key not configured")
        return self._client

    async def search(
        self,
        *,
        objective: str,
        queries: list[str],
        mode: _Types.SearchMode = "fast",
        max_results: int = 5,
        max_chars_total: int = 6000,
    ) -> SearchResult:
        """Search the web and return ranked pages with excerpts relevant to the objective."""
        return await self.client.search(
            objective=objective,
            search_queries=queries,
            mode=mode,
            max_chars_total=max_chars_total,
            advanced_settings={"max_results": max_results},
        )
