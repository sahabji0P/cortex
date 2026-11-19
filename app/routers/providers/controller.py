from fastapi import APIRouter

from app.routers.providers.models import ProvidersModels
from app.routers.providers.service import ProvidersServiceRegistry

# No sign-in, like the gems routes: this backend has one user for now.
router = APIRouter(prefix="/v1/providers", tags=["providers"])


@router.get("/openai")
async def get_openai(service: ProvidersServiceRegistry.V1) -> ProvidersModels.V1.Output.OpenAIRoute:
    """How OpenAI is reached now, and which ways are set up."""
    return await service.get_openai()


@router.put("/openai")
async def set_openai(
    payload: ProvidersModels.V1.Input.SetOpenAIRoute,
    service: ProvidersServiceRegistry.V1,
) -> ProvidersModels.V1.Output.OpenAIRoute:
    """Choose the way. It is kept, and used from the next request to OpenAI on: a call in
    progress uses it from its next reply. A way that is not set up gets 409."""
    return await service.set_openai(payload)
