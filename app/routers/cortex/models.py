from datetime import datetime
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator

from app.adapters.cartesia_stt_adp import CartesiaSTTTurnConfig


class CortexModels:
    class V1:
        class Input:
            class CreateProject(BaseModel):
                name: str = Field(min_length=1, max_length=100)

                @field_validator("name")
                @classmethod
                def validate_name(cls, value: str) -> str:
                    if not (name := value.strip()):
                        raise ValueError("Project name cannot be blank")
                    return name

            class CreateSession(BaseModel):
                project_id: str | None = Field(
                    default=None,
                    validation_alias=AliasChoices("projectId", "project_id"),
                )

            class UpdateSession(BaseModel):
                project_id: str | None = Field(
                    validation_alias=AliasChoices("projectId", "project_id"),
                )

            class TranscribeConfig(BaseModel):
                """Client text frame that retunes turn detection mid-session."""

                type: Literal["config"]
                turn: CartesiaSTTTurnConfig

            class TranscribeClose(BaseModel):
                """Client text frame that flushes buffered audio and ends the session."""

                type: Literal["close"]

        class Output:
            class Project(BaseModel):
                id: str
                name: str
                created_at: datetime
                updated_at: datetime

            class Session(BaseModel):
                id: str
                project_id: str | None
                has_turns: bool
                created_at: datetime
                updated_at: datetime

            class SessionTurn(BaseModel):
                id: str
                provider: str
                parts: list[dict[str, str]]
                created_at: datetime
                updated_at: datetime
                session_id: str
