import asyncio
import contextlib
from typing import Annotated

from fastapi import Depends, WebSocket
from pydantic import ValidationError

from app.entities import EntitiesRegistry
from app.entities.call_ent import CallEntityClass, CallRefused, fault_reason
from app.entities.darbar_ent import DarbarEntityClass
from app.entities.gem_ent import GemEntityClass
from app.entities.voice_ent import VoiceConversation, VoiceEntityClass
from app.routers.voice.models import VoiceModels


class VoiceServiceV1Class:
    def __init__(
        self,
        voice: VoiceEntityClass | None = None,
        gems: GemEntityClass | None = None,
        darbar: DarbarEntityClass | None = None,
    ) -> None:
        # Tests hand in their own. The app uses the ones the registry made at startup.
        self.voice = EntitiesRegistry.Voice if voice is None else voice
        self.gems = EntitiesRegistry.Gems if gems is None else gems
        self.darbar = EntitiesRegistry.Darbar if darbar is None else darbar
        # Opening a call is shared with the standing socket, which carries calls too. It
        # is made from the three above, so a test that hands in its own gets them used.
        self.call = CallEntityClass(gems=self.gems, darbar=self.darbar, voice=self.voice)

    def models(self) -> list[VoiceModels.V1.Output.Model]:
        """Every model a conversation can be held with, in the order to show them."""
        return [
            VoiceModels.V1.Output.Model(
                name=info.name,
                provider=info.provider,
                label=info.label,
                available=available,
                default=default,
                listed=info.listed,
            )
            for info, available, default in self.voice.models()
        ]

    async def converse(self, websocket: WebSocket) -> None:
        """Bridge a client WebSocket to one voice conversation.

        The query string may name a gem to talk to (`?gem=<id>`): its config says who
        answers, with which model and tools, and whether it speaks first. It may name the
        model (`?model=Haiku 5.5`) and who speaks first (`?first=assistant`). Either one
        that is sent wins over the gem's for this call. Without a gem or a model, the
        configured model is used, and without a gem or `first`, the assistant waits for
        the user.

        Uplink: client binary frames are mic audio, and a JSON text frame can report the
        client's playback delay. Downlink: binary frames are speech audio, text frames
        are JSON `VoiceEvent`s. Ends when either side drops.

        This is the old call socket, kept as it is until the desktop app holds its calls
        over `/v1/link`. Opening the call is shared with that socket (`CallEntityClass`).
        """
        await websocket.accept()
        try:
            asked = VoiceModels.V1.Input.Query.model_validate(dict(websocket.query_params))
        except ValidationError:
            # 1008: policy violation — the client asked for a model or a setting that
            # is not offered.
            await websocket.close(code=1008, reason="unknown model or setting")
            return
        # Left out, it is the gem's to say. Without a gem, that means the user.
        first = asked.first == "assistant" if "first" in asked.model_fields_set else None
        try:
            conversation = await self.call.open(asked.gem, model=asked.model, speaks_first=first)
        except CallRefused as exc:
            # 1008 again: no gem with that id (or not an id at all), or the model is
            # offered but its provider has no key here.
            await websocket.close(code=1008, reason=exc.reason)
            return
        except Exception as exc:
            # 1011: internal error — the gem's config cannot be read, or a speech service
            # is unreachable or not configured.
            with contextlib.suppress(Exception):
                await websocket.close(code=1011, reason=fault_reason(exc))
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
            elif (text := message.get("text")) is not None:
                # The only text frame a client sends. Anything else is ignored.
                with contextlib.suppress(ValidationError):
                    report = VoiceModels.V1.Input.Playback.model_validate_json(text)
                    conversation.set_output_delay(report.delay)

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
