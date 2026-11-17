from typing import Annotated

from fastapi import Depends

from app.entities import EntitiesRegistry
from app.entities.voice_ent import VoiceEntityClass
from app.routers.voice.models import VoiceModels


class VoiceServiceV1Class:
    def __init__(self, voice: VoiceEntityClass | None = None) -> None:
        # Tests hand in their own. The app uses the one the registry made at startup.
        self.voice = EntitiesRegistry.Voice if voice is None else voice

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


_singleton: VoiceServiceV1Class | None = None


def get_voice_service_v1() -> VoiceServiceV1Class:
    global _singleton  # noqa: PLW0603
    if _singleton is None:
        _singleton = VoiceServiceV1Class()
    return _singleton


class VoiceServiceRegistry:
    type V1 = Annotated[VoiceServiceV1Class, Depends(get_voice_service_v1)]
