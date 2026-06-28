# Official documented import path; the SDK re-exports it but omits it from __all__,
# which trips pyright's reportPrivateImportUsage in strict mode.
from anthropic import AsyncAnthropicVertex  # pyright: ignore[reportPrivateImportUsage]

from app.config import settings


class AnthropicAdapterClass:
    """Claude via Vertex AI. Auth is GCP ADC — no Anthropic API key."""

    def __init__(self) -> None:
        self._client: AsyncAnthropicVertex | None = None

    async def initialize(self) -> None:
        self._client = AsyncAnthropicVertex(
            project_id=settings.gcp_project_id,
            region=settings.vertex_region,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()

    @property
    def client(self) -> AsyncAnthropicVertex:
        if self._client is None:
            raise RuntimeError("Anthropic Vertex client not initialized")
        return self._client
