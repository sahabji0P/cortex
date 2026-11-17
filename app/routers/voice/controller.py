from fastapi import APIRouter

from app.routers.voice.models import VoiceModels
from app.routers.voice.service import VoiceServiceRegistry

router = APIRouter(prefix="/v1/voice", tags=["voice"])


@router.get("/models")
async def models(service: VoiceServiceRegistry.V1) -> list[VoiceModels.V1.Output.Model]:
    """The models a conversation can be held with, and which of them have a key set."""
    return service.models()
