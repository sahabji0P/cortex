from typing import Literal

from pydantic import BaseModel, Field

from app.entities.voice_model import VoiceModelName


class VoiceModels:
    class V1:
        class Input:
            class Query(BaseModel):
                """The WebSocket's query string. Anything else in it is ignored."""

                model: VoiceModelName | None = None

            class Playback(BaseModel):
                """Client text frame reporting how long its sound device delays audio.

                In a browser this is `AudioContext.outputLatency + baseLatency`. It lets
                the server work out what the user has really heard when they interrupt.
                """

                type: Literal["playback"]
                delay: float = Field(ge=0)
