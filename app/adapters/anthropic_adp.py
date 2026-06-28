# Official documented import path; the SDK re-exports it but omits it from __all__,
# which trips pyright's reportPrivateImportUsage in strict mode.
from types import MappingProxyType
from typing import Literal

from anthropic import AsyncAnthropicVertex  # pyright: ignore[reportPrivateImportUsage]

from app.config import settings


class _Types:
    """Group private Anthropic adapter model types."""

    type AvailableModels = Literal["Haiku 4.5", "Sonnet 4.6"]
    model_map = MappingProxyType[AvailableModels, str](
        {
            "Haiku 4.5": "claude-haiku-4-5",
            "Sonnet 4.6": "claude-sonnet-4-6",
        }
    )


class AnthropicAdapterClass:
    """Claude via Vertex AI. Auth is GCP ADC — no Anthropic API key."""

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

    @staticmethod
    def model_slug(model: _Types.AvailableModels) -> str:
        """Resolve an Anthropic app-facing model label to its provider slug."""
        return _Types.model_map[model]

    @property
    def client(self) -> AsyncAnthropicVertex:
        """Return the initialized Anthropic Vertex client."""
        if self._client is None:
            raise RuntimeError("Anthropic Vertex client not initialized")
        return self._client
