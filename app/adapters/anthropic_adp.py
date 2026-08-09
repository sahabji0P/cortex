# Official documented import path; the SDK re-exports it but omits it from __all__,
# which trips pyright's reportPrivateImportUsage in strict mode.
from collections.abc import Iterable
from types import MappingProxyType
from typing import Literal

from anthropic import (
    AsyncAnthropicVertex,  # pyright: ignore[reportPrivateImportUsage]
    AsyncStream,
    Omit,
    omit,
)
from anthropic.types import (
    MessageParam,
    RawMessageStreamEvent,
    ThinkingConfigParam,
    ToolUnionParam,
    WebSearchTool20250305Param,
    web_search_tool_20250305_param,
)

from app.config import settings

__all__ = ["AnthropicAdapterClass", "AnthropicModel"]

# Public so the cortex router can type its payload field; Vertex serves Claude with
# bare first-party IDs for current-generation models and `@`-dated snapshot IDs.
type AnthropicModel = Literal["Haiku 4.5", "Sonnet 5"]


class _Types:
    """Group private Anthropic adapter model types."""

    model_map = MappingProxyType[AnthropicModel, str](
        {
            "Haiku 4.5": "claude-haiku-4-5@20251001",
            "Sonnet 5": "claude-sonnet-5",
        }
    )


class AnthropicAdapterClass:
    """Claude via Vertex AI. Auth is GCP ADC — no Anthropic API key."""

    @staticmethod
    def build_text_messages(
        messages: Iterable[tuple[Literal["user", "assistant"], str]],
        prompt: str,
    ) -> list[MessageParam]:
        """Build provider-native Messages input from normalized persisted text."""
        result: list[MessageParam] = [
            {"role": role, "content": content} for role, content in messages
        ]
        result.append({"role": "user", "content": prompt})
        return result

    class Tools:
        """Server-side tool builders for the Messages API.

        Vertex AI serves only the basic `web_search_20250305` variant; web fetch and
        code execution are not available on Vertex (first-party API only).
        """

        @staticmethod
        def web_search(
            *,
            allowed_domains: list[str] | None | Omit = omit,
            blocked_domains: list[str] | None | Omit = omit,
            max_uses: int | None | Omit = omit,
            user_location: web_search_tool_20250305_param.UserLocation | None | Omit = omit,
        ) -> WebSearchTool20250305Param:
            """Web search built-in tool (basic variant — the only one on Vertex)."""
            tool: WebSearchTool20250305Param = {
                "type": "web_search_20250305",
                "name": "web_search",
            }

            if not isinstance(allowed_domains, Omit):
                tool["allowed_domains"] = allowed_domains
            if not isinstance(blocked_domains, Omit):
                tool["blocked_domains"] = blocked_domains
            if not isinstance(max_uses, Omit):
                tool["max_uses"] = max_uses
            if not isinstance(user_location, Omit):
                tool["user_location"] = user_location

            return tool

    def __init__(self) -> None:
        """Create an empty Anthropic Vertex async client holder."""
        self._client: AsyncAnthropicVertex | None = None

    async def initialize(self) -> None:
        """Initialize the Anthropic Vertex async client."""
        self._client = AsyncAnthropicVertex(
            project_id=settings.gcp_project_id,
            region=settings.vertex_region,
        )

    async def close(self) -> None:
        """Close the Anthropic Vertex client if it was initialized."""
        if self._client is not None:
            await self._client.close()

    async def stream_messages(
        self,
        *,
        model: AnthropicModel,
        input: str | Iterable[MessageParam],
        max_tokens: int,
        system: str | Omit = omit,
        thinking: ThinkingConfigParam | Omit = omit,
        tools: Iterable[ToolUnionParam] | Omit = omit,
    ) -> AsyncStream[RawMessageStreamEvent]:
        """Create a streamed Anthropic Messages API request and return raw SDK events."""
        messages: Iterable[MessageParam] = (
            [{"role": "user", "content": input}] if isinstance(input, str) else input
        )
        return await self.client.messages.create(
            model=_Types.model_map[model],
            messages=messages,
            max_tokens=max_tokens,
            system=system,
            thinking=thinking,
            tools=tools,
            stream=True,
        )

    @property
    def client(self) -> AsyncAnthropicVertex:
        """Return the initialized Anthropic Vertex client."""
        if self._client is None:
            raise RuntimeError("Anthropic Vertex client not initialized")
        return self._client
