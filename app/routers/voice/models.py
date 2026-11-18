from pydantic import BaseModel

from app.entities.model import ModelName, Provider


class VoiceModels:
    class V1:
        class Output:
            class Model(BaseModel):
                """One model a conversation can be held with."""

                name: ModelName  # what a gem's `model` names
                provider: Provider
                label: str
                # Whether the provider's key is set. Without it a conversation cannot open.
                available: bool
                # Whether this is the model used when a gem names none.
                default: bool
                # Whether a model picker should offer it. Others still work by name.
                listed: bool
