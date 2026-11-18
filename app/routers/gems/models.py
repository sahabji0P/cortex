from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.entities.gem_ent import (
    DEFAULT_GEM_TOOLS,
    GemInstructions,
    GemModelName,
    GemName,
    GemTools,
)


class GemsModels:
    class V1:
        class Input:
            class CreateGem(BaseModel):
                name: GemName
                instructions: GemInstructions = ""
                # A name from `GET /v1/voice/models`, or none for the configured default.
                model: GemModelName | None = None
                speaks_first: bool = False
                tools: GemTools = Field(default_factory=lambda: list(DEFAULT_GEM_TOOLS))

            class UpdateGem(BaseModel):
                """A partial change. A field left out keeps its value.

                Only `model` may be sent as null, which means the configured default.
                """

                name: GemName | None = None
                instructions: GemInstructions | None = None
                model: GemModelName | None = None
                speaks_first: bool | None = None
                tools: GemTools | None = None

                @model_validator(mode="after")
                def _refuse_null(self) -> "GemsModels.V1.Input.UpdateGem":
                    for field in self.model_fields_set - {"model"}:
                        if getattr(self, field) is None:
                            raise ValueError(f"A gem's {field} cannot be null")
                    return self

                def changes(self) -> dict[str, object]:
                    """Only the fields that were sent, so a null `model` is kept."""
                    return self.model_dump(exclude_unset=True)

        class Output:
            class Gem(BaseModel):
                id: str
                version: int
                name: str
                instructions: str
                model: GemModelName | None
                speaks_first: bool
                tools: list[str]
                created_at: datetime
                updated_at: datetime
