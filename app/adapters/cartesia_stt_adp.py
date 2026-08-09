"""
Cartesia adapter responsible for realtime speech-to-text over the turns WebSocket API.

Responsibilities:
- Manage the async Cartesia client lifecycle
- Open auto-finalize (turns) STT WebSocket sessions that emit typed turn events
- Validate and apply turn-detection thresholds, at connect time and mid-session

See https://docs.cartesia.ai/api-reference/stt/turns/websocket for the wire protocol.
"""

import json
from collections.abc import AsyncIterator
from typing import Self

from cartesia import AsyncCartesia
from cartesia.resources.stt.auto_finalize import AsyncAutoFinalizeResourceConnection
from cartesia.types import STTEncoding
from cartesia.types.stt import (
    AutoFinalizeWebsocketParams,
    STTAutoFinalizeModel,
    STTAutoFinalizeWebsocketRequestParam,
    STTAutoFinalizeWebsocketResponse,
)
from pydantic import BaseModel, Field, model_validator

from app.config import settings

# Cartesia SDK types re-exported (see __all__) so entities can build websocket params,
# reconnect hooks, and narrow received events without importing `cartesia.*` themselves —
# the adapter stays the SDK boundary.
__all__ = [
    "AutoFinalizeWebsocketParams",
    "CartesiaSTTAdapterClass",
    "CartesiaSTTSession",
    "CartesiaSTTTurnConfig",
    "STTAutoFinalizeModel",
    "STTAutoFinalizeWebsocketResponse",
    "STTEncoding",
]

# The turns API is documented against a newer API version than the one the SDK
# (cartesia==3.3.0) pins by default, so it is overridden here. Re-verify on SDK upgrade.
_CARTESIA_VERSION = "2026-03-01"
_DEFAULT_MODEL: STTAutoFinalizeModel = "ink-2"
_DEFAULT_ENCODING: STTEncoding = "pcm_s16le"
_DEFAULT_SAMPLE_RATE = 16000

# Server-side defaults and allowed ranges per the turns WebSocket API reference.
_DEFAULT_TURN_START_THRESHOLD = 0.8
_DEFAULT_TURN_EAGER_END_THRESHOLD = 0.4
_DEFAULT_TURN_END_THRESHOLD = 0.2
_DEFAULT_TURN_END_TIMEOUT_MS = 5600


class CartesiaSTTTurnConfig(BaseModel):
    """Turn-detection settings supported by Cartesia's realtime STT turns API."""

    start_threshold: float = Field(default=_DEFAULT_TURN_START_THRESHOLD, ge=0.5, le=0.9)
    eager_end_threshold: float = Field(
        default=_DEFAULT_TURN_EAGER_END_THRESHOLD,
        ge=0.3,
        le=0.6,
    )
    end_threshold: float = Field(default=_DEFAULT_TURN_END_THRESHOLD, ge=0.05, le=0.5)
    end_timeout_ms: int = Field(default=_DEFAULT_TURN_END_TIMEOUT_MS, ge=640, le=11200)

    @model_validator(mode="after")
    def validate_threshold_order(self) -> Self:
        if not self.start_threshold > self.eager_end_threshold > self.end_threshold:
            raise ValueError(
                "Cartesia turn thresholds must satisfy "
                "start_threshold > eager_end_threshold > end_threshold"
            )
        return self

    def to_query(self) -> dict[str, object]:
        """Render as the `turn_*` query parameters used when opening a connection."""
        return {
            "turn_start_threshold": self.start_threshold,
            "turn_eager_end_threshold": self.eager_end_threshold,
            "turn_end_threshold": self.end_threshold,
            "turn_end_timeout_ms": self.end_timeout_ms,
        }

    def to_config_command(self) -> dict[str, object]:
        """Render as the `config` text frame used to retune a live session."""
        # The model's field names are the wire names, so the dump is the payload.
        return {"type": "config", "turn": self.model_dump()}


class CartesiaSTTSession:
    """Small lifecycle wrapper around Cartesia's typed STT WebSocket connection."""

    def __init__(self, connection: AsyncAutoFinalizeResourceConnection) -> None:
        self._connection = connection

    async def send_audio(self, audio: bytes) -> None:
        """Send one raw audio chunk matching the connection encoding and sample rate."""
        await self._connection.send_raw(audio)

    async def update_turn_config(self, config: CartesiaSTTTurnConfig) -> None:
        """Update turn-detection thresholds mid-session."""
        # The SDK's typed request union only covers `close`, so the `config` command
        # goes out as a raw text frame built from our own wire-shape TypedDict.
        await self._connection.send_raw(json.dumps(config.to_config_command()))

    async def close_input(self) -> None:
        """Flush buffered audio and ask Cartesia to close after final events."""
        command: STTAutoFinalizeWebsocketRequestParam = {"type": "close"}
        await self._connection.send(command)

    async def events(self) -> AsyncIterator[STTAutoFinalizeWebsocketResponse]:
        """Iterate typed Cartesia STT events until the server closes the socket."""
        async for event in self._connection:
            yield event

    async def close(self) -> None:
        """Close the underlying WebSocket immediately."""
        await self._connection.close()


class CartesiaSTTAdapterClass:
    """Wrap the async Cartesia client for realtime STT sessions with turn detection."""

    def __init__(self) -> None:
        self._client: AsyncCartesia | None = None

    async def initialize(self) -> None:
        if not settings.cartesia_api_key:
            return

        # The version header rides on every request including the WebSocket handshake,
        # so it is the single place the API version is pinned.
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

    @staticmethod
    def default_websocket_params() -> AutoFinalizeWebsocketParams:
        return {
            "encoding": _DEFAULT_ENCODING,
            "model": _DEFAULT_MODEL,
            "sample_rate": _DEFAULT_SAMPLE_RATE,
        }

    async def connect_auto_turns(
        self,
        *,
        websocket_params: AutoFinalizeWebsocketParams | None = None,
        turn_config: CartesiaSTTTurnConfig | None = None,
    ) -> CartesiaSTTSession:
        """
        Open a Cartesia auto-turn STT session.

        Args:
            websocket_params: Model, encoding, and sample rate for the session.
                Defaults to `default_websocket_params()`.
            turn_config: Turn-detection thresholds applied at connect time via query
                parameters. Defaults to Cartesia's documented server defaults.

        Returns:
            A `CartesiaSTTSession` ready to receive audio. The caller owns its
            lifecycle and must `close_input()` / `close()` it.

        """
        params = websocket_params or self.default_websocket_params()
        config = turn_config or CartesiaSTTTurnConfig()
        connection = await self.client.stt.auto_finalize.websocket(
            encoding=params["encoding"],
            model=params["model"],
            sample_rate=params["sample_rate"],
            extra_query=config.to_query(),
        ).enter()
        return CartesiaSTTSession(connection)
