from collections.abc import AsyncIterator

from google import genai
from google.genai import types
from google.genai.client import AsyncClient

from app.config import settings


class GeminiAdapterClass:
    """Gemini via Vertex AI. Auth is GCP ADC — shares project/region with Anthropic.

    `.client` is the async client; `.stream(...)` wraps the SDK's streaming call.
    """

    def __init__(self) -> None:
        self._client: AsyncClient | None = None

    async def initialize(self) -> None:
        # AsyncClient is only reachable via Client(...).aio — keep it, drop the sync wrapper.
        self._client = genai.Client(
            vertexai=True,
            project=settings.gcp_project_id,
            location=settings.vertex_region,
        ).aio

    async def close(self) -> None:
        # google-genai holds no long-lived connection that needs explicit teardown.
        return

    @property
    def client(self) -> AsyncClient:
        if self._client is None:
            raise RuntimeError("Gemini client not initialized")
        return self._client

    @staticmethod
    def builtin_tools() -> list[types.Tool]:
        """Gemini server-side tools: Google Search grounding, code execution, URL context."""
        return [
            types.Tool(google_search=types.GoogleSearch()),
            types.Tool(code_execution=types.ToolCodeExecution()),
            types.Tool(url_context=types.UrlContext()),
        ]

    async def stream(
        self,
        # ContentListUnion is the SDK's own type; its union has an untyped (PIL) member.
        contents: types.ContentListUnion,  # pyright: ignore[reportUnknownParameterType]
        *,
        model: str | None = None,
        tools: list[types.Tool] | None = None,
        config: types.GenerateContentConfig | None = None,
    ) -> AsyncIterator[types.GenerateContentResponse]:
        """Stream a generation. Yields raw SDK chunks so callers can read text,
        tool calls, and grounding metadata. Pass `tools=GeminiAdapterClass.builtin_tools()`
        to enable the server-side tools, or a custom `config` to control everything.
        """
        if config is None:
            config = types.GenerateContentConfig(tools=tools)

        stream = await self.client.models.generate_content_stream(
            model=model or settings.gemini_model,
            contents=contents,
            config=config,
        )
        async for chunk in stream:
            yield chunk
