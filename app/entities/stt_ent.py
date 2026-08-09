"""
STT entity: realtime transcription sessions with turn detection on top of Cartesia.

Sits between the cortex service layer and the Cartesia adapter — callers get a
`CartesiaSTTSession` plus wire-ready event serialization without touching the SDK.
"""

from app.adapters.cartesia_stt_adp import (
    CartesiaSTTAdapterClass,
    CartesiaSTTSession,
    CartesiaSTTTurnConfig,
    STTAutoFinalizeModel,
    STTAutoFinalizeWebsocketResponse,
    STTEncoding,
)

__all__ = ["STTEntityClass"]


class STTEntityClass:
    def __init__(self, *, stt: CartesiaSTTAdapterClass) -> None:
        self._stt = stt

    async def open_turns_session(
        self,
        *,
        model: STTAutoFinalizeModel | None = None,
        encoding: STTEncoding | None = None,
        sample_rate: int | None = None,
        turn_config: CartesiaSTTTurnConfig | None = None,
    ) -> CartesiaSTTSession:
        """Open a realtime STT session, falling back to adapter defaults per field.

        The caller owns the returned session and must `close_input()` / `close()` it.
        """
        params = self._stt.default_websocket_params()
        if model is not None:
            params["model"] = model
        if encoding is not None:
            params["encoding"] = encoding
        if sample_rate is not None:
            params["sample_rate"] = sample_rate
        return await self._stt.connect_auto_turns(
            websocket_params=params,
            turn_config=turn_config,
        )

    @staticmethod
    def serialize_event(event: STTAutoFinalizeWebsocketResponse) -> str:
        """Serialize a typed Cartesia event to compact JSON exactly as the API shapes it."""
        return event.to_json(indent=None)
