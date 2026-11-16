"""
Cartesia adapter responsible for streaming text-to-speech over the contexts WebSocket API.

Responsibilities:
- Manage the async Cartesia client lifecycle
- Open one TTS WebSocket per conversation
- Run one context per utterance: text fragments in, raw PCM audio and word timings
  out, cancellable

See https://docs.cartesia.ai/use-the-api/tts-websocket/contexts for the wire protocol.
"""

from collections.abc import AsyncIterator
from types import MappingProxyType
from typing import Literal, NamedTuple

from cartesia import AsyncCartesia
from cartesia.resources.tts import AsyncTTSResourceConnection, AsyncWebSocketContext

from app.config import settings

__all__ = [
    "CartesiaTTSAdapterClass",
    "CartesiaTTSContext",
    "CartesiaTTSSession",
    "CartesiaTTSVoice",
    "CartesiaTTSWord",
]

# Same API version the STT adapter pins. Re-verify on SDK upgrade.
_CARTESIA_VERSION = "2026-03-01"
# Seconds without any event from Cartesia before a context gives up.
_CONTEXT_TIMEOUT = 30.0

type CartesiaTTSVoice = Literal["female", "male"]


class CartesiaTTSWord(NamedTuple):
    """One word of an utterance, as Cartesia timed it."""

    text: str
    end: float  # seconds from the start of the utterance's audio


class _Types:
    """Group private Cartesia TTS model, voice and audio settings."""

    type AvailableModels = Literal["Sonic 3.6"]
    model_map = MappingProxyType[AvailableModels, str]({"Sonic 3.6": "sonic-3.6"})
    voice_map = MappingProxyType[CartesiaTTSVoice, str](
        {
            "female": "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4",
            "male": "630ed21c-2c5c-41cf-9d82-10a7fd668370",
        }
    )
    # Raw 16-bit mono PCM: no container, so chunks can be played as they arrive.
    sample_rate = 24000
    output_format = MappingProxyType(
        {"container": "raw", "encoding": "pcm_s16le", "sample_rate": sample_rate}
    )


class CartesiaTTSContext:
    """One utterance: push text fragments in order, iterate audio and word timings out."""

    def __init__(self, context: AsyncWebSocketContext) -> None:
        self._context = context

    async def push(self, text: str) -> None:
        """Add the next fragment of text. Cartesia joins fragments into one utterance."""
        await self._context.push(text)

    async def finish(self) -> None:
        """Say that no more text is coming; the remaining audio still arrives."""
        await self._context.no_more_inputs()

    async def events(self) -> AsyncIterator[bytes | list[CartesiaTTSWord]]:
        """Yield PCM chunks and word timings, as they arrive, until the utterance is done.

        The words are the whitespace-separated words of the pushed text, in order. Each
        timing arrives about when the audio for that word does.
        """
        async for event in self._context.receive():
            if event.type == "chunk":
                if event.audio:
                    yield event.audio
            elif event.type == "timestamps":
                if timings := event.word_timestamps:
                    yield [
                        CartesiaTTSWord(text, end)
                        for text, end in zip(timings.words, timings.end, strict=True)
                    ]
            elif event.type == "error":
                raise RuntimeError(f"Cartesia speech synthesis failed: {event.error}")

    async def cancel(self) -> None:
        """Stop generating this utterance now."""
        await self._context.cancel()


class CartesiaTTSSession:
    """One TTS WebSocket, owned by one conversation."""

    def __init__(self, connection: AsyncTTSResourceConnection, *, model: str, voice: str) -> None:
        self._connection = connection
        self._model = model
        self._voice = voice

    def context(self) -> CartesiaTTSContext:
        """Start a new utterance on this connection."""
        return CartesiaTTSContext(
            self._connection.context(
                model_id=self._model,
                voice={"mode": "id", "id": self._voice},
                output_format=_Types.output_format,
                add_timestamps=True,
                timeout=_CONTEXT_TIMEOUT,
            )
        )

    async def close(self) -> None:
        """Close the underlying WebSocket."""
        await self._connection.close()


class CartesiaTTSAdapterClass:
    """Wrap the async Cartesia client for streaming speech synthesis."""

    sample_rate = _Types.sample_rate

    def __init__(self) -> None:
        self._client: AsyncCartesia | None = None

    async def initialize(self) -> None:
        if not settings.cartesia_api_key:
            return

        self._client = AsyncCartesia(
            api_key=settings.cartesia_api_key,
            default_headers={"cartesia-version": _CARTESIA_VERSION},
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()

    @property
    def client(self) -> AsyncCartesia:
        if self._client is None:
            raise RuntimeError("Cartesia client not initialized or API key not configured")
        return self._client

    async def connect(
        self,
        *,
        model: _Types.AvailableModels = "Sonic 3.6",
        voice: CartesiaTTSVoice = "female",
    ) -> CartesiaTTSSession:
        """Open a TTS WebSocket. The caller owns the session and must `close()` it."""
        connection = await self.client.tts.websocket_connect().enter()
        return CartesiaTTSSession(
            connection,
            model=_Types.model_map[model],
            voice=_Types.voice_map[voice],
        )
