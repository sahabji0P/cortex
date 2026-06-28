from collections.abc import AsyncIterator
from types import MappingProxyType
from typing import Literal

from google import genai
from google.genai import types
from google.genai.client import AsyncClient

from app.config import settings


class _Types:
    """Group private Gemini adapter model types."""

    type AvailableModels = Literal["3.5 Flash", "3.1 Flash Lite"]
    model_map = MappingProxyType[AvailableModels, str](
        {
            "3.5 Flash": "gemini-3.5-flash",
            "3.1 Flash Lite": "gemini-3.1-flash-lite",
        }
    )


class GeminiAdapterClass:
    """Gemini via Vertex AI. Auth is GCP ADC — shares project/region with Anthropic.

    `.client` is the async client; `.stream(...)` wraps the SDK's streaming call.
    """

    def __init__(self) -> None:
        """Create an empty Gemini async client holder."""
        self._client: AsyncClient | None = None

    async def initialize(self) -> None:
        """Initialize the Gemini async client for Vertex AI."""
        # AsyncClient is only reachable via Client(...).aio — keep it, drop the sync wrapper.
        self._client = genai.Client(
            vertexai=True,
            project=settings.gcp_project_id,
            location=settings.vertex_region,
        ).aio

    async def close(self) -> None:
        """Complete Gemini adapter shutdown."""
        # google-genai holds no long-lived connection that needs explicit teardown.
        return

    @property
    def client(self) -> AsyncClient:
        """Return the initialized Gemini async client."""
        if self._client is None:
            raise RuntimeError("Gemini client not initialized")
        return self._client

    async def stream(
        self,
        # ContentListUnion is the SDK's own type; its union has an untyped (PIL) member.
        contents: types.ContentListUnion,  # pyright: ignore[reportUnknownParameterType]
        *,
        model: _Types.AvailableModels,
        tools: list[types.Tool] | None = None,
        config: types.GenerateContentConfig | None = None,
    ) -> AsyncIterator[types.GenerateContentResponse]:
        """Stream Gemini generation chunks with raw SDK response metadata."""
        if config is None:
            config = types.GenerateContentConfig(tools=tools)

        stream = await self.client.models.generate_content_stream(
            model=_Types.model_map[model],
            contents=contents,
            config=config,
        )
        async for chunk in stream:
            yield chunk
