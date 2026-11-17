from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.entities.gem_ent import (
    DEFAULT_GEM_TOOLS,
    GemDescription,
    GemImage,
    GemInstructions,
    GemModelName,
    GemName,
    GemSpeechSpeed,
    GemToolsToSave,
    GemVoice,
)


class GemsModels:
    class V1:
        class Input:
            class CreateGem(BaseModel):
                name: GemName
                description: GemDescription = ""
                image: GemImage | None = None
                instructions: GemInstructions = ""
                # A name from `GET /v1/voice/models`, or none for the configured default.
                model: GemModelName | None = None
                speaks_first: bool = False
                tools: GemToolsToSave = Field(default_factory=lambda: list(DEFAULT_GEM_TOOLS))
                voice: GemVoice | None = None
                speech_speed: GemSpeechSpeed = 1.0

            class UpdateGem(BaseModel):
                """A partial change. A field left out keeps its value.

                `model`, `image` and `voice` may be sent as null: the configured default
                model, no picture, and no voice chosen. Nothing else may.
                """

                name: GemName | None = None
                description: GemDescription | None = None
                image: GemImage | None = None
                instructions: GemInstructions | None = None
                model: GemModelName | None = None
                speaks_first: bool | None = None
                tools: GemToolsToSave | None = None
                voice: GemVoice | None = None
                speech_speed: GemSpeechSpeed | None = None

                @model_validator(mode="after")
                def _refuse_null(self) -> "GemsModels.V1.Input.UpdateGem":
                    for field in self.model_fields_set - {"model", "image", "voice"}:
                        if getattr(self, field) is None:
                            raise ValueError(f"A gem's {field} cannot be null")
                    return self

                def changes(self) -> dict[str, object]:
                    """Only the fields that were sent, so a null that was sent is kept."""
                    return self.model_dump(exclude_unset=True)

        class Output:
            class Gem(BaseModel):
                id: str
                version: int
                name: str
                description: str
                image: str | None
                instructions: str
                model: GemModelName | None
                speaks_first: bool
                tools: list[str]
                voice: str | None
                speech_speed: float
                created_at: datetime
                updated_at: datetime

            class ChatMessage(BaseModel):
                # `tool`: the gem used a tool. `text` is what its `chat.tool` frame said.
                role: Literal["user", "gem", "tool"]
                # A reply that was cut ends in "…".
                text: str
                at: datetime

            class Chat(BaseModel):
                """A gem's written conversation so far, oldest first."""

                messages: list["GemsModels.V1.Output.ChatMessage"]
