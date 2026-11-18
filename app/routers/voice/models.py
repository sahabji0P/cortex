from typing import Literal

from pydantic import BaseModel, Field

from app.entities.model import ModelName, Provider


class VoiceModels:
    class V1:
        class Input:
            class Query(BaseModel):
                """The WebSocket's query string. Anything else in it is ignored."""

                # The id of the gem to talk to. Its config says who answers and how.
                gem: str | None = None
                model: ModelName | None = None
                # Who says the first words. By default the assistant waits for the user,
                # or does what the gem says. Whether it was sent at all is told apart by
                # `model_fields_set`, since a value sent wins over the gem's.
                first: Literal["user", "assistant"] = "user"

            class Playback(BaseModel):
                """Client text frame reporting how long its sound device delays audio.

                In a browser this is `AudioContext.outputLatency + baseLatency`. It lets
                the server work out what the user has really heard when they interrupt.
                """

                type: Literal["playback"]
                delay: float = Field(ge=0)

        class Output:
            class Model(BaseModel):
                """One model a conversation can be held with."""

                name: ModelName  # what to send as `?model=`
                provider: Provider
                label: str
                # Whether the provider's key is set. Without it a conversation cannot open.
                available: bool
                # Whether this is the model used when a client names none.
                default: bool
                # Whether a model picker should offer it. Others still work by name.
                listed: bool
