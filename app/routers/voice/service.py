import asyncio
import contextlib
from typing import Annotated

from fastapi import Depends, WebSocket

from app.entities import EntitiesRegistry
from app.entities.voice_ent import VoiceConversation


class VoiceServiceV1Class:
    def __init__(self) -> None:
        self.voice = EntitiesRegistry.Voice

    async def converse(self, websocket: WebSocket) -> None:
        """Bridge a client WebSocket to one voice conversation.

        Uplink: client binary frames are mic audio. Downlink: binary frames are speech
        audio, text frames are JSON `VoiceEvent`s. Ends when either side drops.
        """
        await websocket.accept()
        try:
            conversation = await self.voice.open_conversation()
        except Exception:
            # 1011: internal error — a speech service is unreachable or not configured.
            with contextlib.suppress(Exception):
                await websocket.close(code=1011, reason="speech services unavailable")
            raise

        uplink = asyncio.create_task(self._relay_mic(websocket, conversation))
        downlink = asyncio.create_task(self._relay_outputs(websocket, conversation))
        try:
            done, _pending = await asyncio.wait(
                {uplink, downlink},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                if (exc := task.exception()) is not None:
                    raise exc
        finally:
            # asyncio.wait does not cancel its tasks if this coroutine is cancelled, so
            # both relays and the conversation are cleaned up unconditionally.
            for task in (uplink, downlink):
                task.cancel()
            await asyncio.gather(uplink, downlink, return_exceptions=True)
            await conversation.close()
            with contextlib.suppress(Exception):
                await websocket.close()

    @staticmethod
    async def _relay_mic(websocket: WebSocket, conversation: VoiceConversation) -> None:
        """Pump mic audio into the conversation until the client disconnects."""
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            if (audio := message.get("bytes")) is not None:
                await conversation.send_audio(audio)

    @staticmethod
    async def _relay_outputs(websocket: WebSocket, conversation: VoiceConversation) -> None:
        """Forward speech audio and events to the client until the conversation ends."""
        async for output in conversation.outputs():
            if isinstance(output, bytes):
                await websocket.send_bytes(output)
            else:
                await websocket.send_text(output.model_dump_json())


_singleton: VoiceServiceV1Class | None = None


def get_voice_service_v1() -> VoiceServiceV1Class:
    global _singleton  # noqa: PLW0603
    if _singleton is None:
        _singleton = VoiceServiceV1Class()
    return _singleton


class VoiceServiceRegistry:
    type V1 = Annotated[VoiceServiceV1Class, Depends(get_voice_service_v1)]
