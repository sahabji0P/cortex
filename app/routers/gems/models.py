from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.entities.gem_ent import (
    DEFAULT_GEM_COMPACTION,
    DEFAULT_GEM_TOOLS,
    GemCompaction,
    GemDescription,
    GemImage,
    GemInstructionsToSave,
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
                instructions: GemInstructionsToSave = ""
                # A name from `GET /v1/voice/models`, or none for the configured default.
                model: GemModelName | None = None
                speaks_first: bool = False
                tools: GemToolsToSave = Field(default_factory=lambda: list(DEFAULT_GEM_TOOLS))
                # When its conversation is compacted: `before_expiry`, `on_return` or `never`.
                compaction: GemCompaction = DEFAULT_GEM_COMPACTION
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
                instructions: GemInstructionsToSave | None = None
                model: GemModelName | None = None
                speaks_first: bool | None = None
                tools: GemToolsToSave | None = None
                compaction: GemCompaction | None = None
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
                compaction: GemCompaction
                voice: str | None
                speech_speed: float
                created_at: datetime
                updated_at: datetime

            class ChatMessage(BaseModel):
                # `tool`: the gem used a tool. `text` is what its `chat.tool` frame said.
                # `event`: an event one of the gem's watches caught. `text` is what its
                # `chat.event` frame said.
                role: Literal["user", "gem", "tool", "event"]
                # A reply that was cut ends in "…".
                text: str
                at: datetime

            class Chat(BaseModel):
                """A gem's conversation so far, a call's turns included, oldest first."""

                messages: list["GemsModels.V1.Output.ChatMessage"]

            class Log(BaseModel):
                """A page of a gem's log, oldest first.

                Every entry has `seq` (its number in the log), `at`, `kind` and `turn`,
                then its own fields by kind (see `docs/chat-contract.md`). `settings` is
                the `model`, `instructions` and `tools` in force just before the page's
                first entry, each as a `settings.changed` has it, or null if none was
                written yet.
                """

                entries: list[dict[str, Any]]
                settings: dict[str, Any]
