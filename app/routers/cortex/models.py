from datetime import datetime
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator

from app.adapters.anthropic_adp import AnthropicModel
from app.adapters.cartesia_stt_adp import CartesiaSTTTurnConfig
from app.adapters.gemini_adp import GeminiBuiltinTool
from app.adapters.openai_adp import OpenAIThinking
from app.entities.pipeline_llm import GeminiMode, Provider


class CortexModels:
    class V1:
        class Input:
            class Listen(BaseModel):
                prompt: str
                providers: list[Provider] = Field(min_length=1)
                gemini_mode: GeminiMode = Field(
                    default="generate_content",
                    validation_alias=AliasChoices("geminiMode", "gemini_mode"),
                )
                gemini_tools: list[GeminiBuiltinTool] = Field(
                    default_factory=list,
                    validation_alias=AliasChoices("geminiTools", "gemini_tools"),
                )
                anthropic_model: AnthropicModel = Field(
                    default="Sonnet 5",
                    validation_alias=AliasChoices("anthropicModel", "anthropic_model"),
                )
                openai_thinking: OpenAIThinking | None = Field(
                    default=None,
                    validation_alias=AliasChoices(
                        "openaiThinking",
                        "openai_thinking",
                        "thinking",
                    ),
                )
                session_id: str | None = Field(
                    default=None,
                    validation_alias=AliasChoices("sessionId", "session_id"),
                )
                project_id: str | None = Field(
                    default=None,
                    validation_alias=AliasChoices("projectId", "project_id"),
                )

                @field_validator("prompt")
                @classmethod
                def validate_prompt(cls, value: str) -> str:
                    if not value.strip():
                        raise ValueError("Prompt cannot be blank")
                    return value

                @field_validator("providers")
                @classmethod
                def validate_providers(cls, value: list[Provider]) -> list[Provider]:
                    if len(value) != len(set(value)):
                        raise ValueError("Providers cannot contain duplicates")
                    return value

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
