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


@router.post("/openai/sign-in")
async def start_sign_in(
    service: ProvidersServiceRegistry.V1,
) -> ProvidersModels.V1.Output.SignInStarted:
    """Start a sign-in to the proxy with a ChatGPT account. The app opens `url` in the
    browser. When it is finished, that account replaces the proxy's other ChatGPT ones.
    409 when the proxy's management key is not set up, 502 when the proxy does not answer."""
    return await service.start_sign_in()


@router.get("/openai/sign-in/{state}")
async def get_sign_in(
    state: str, service: ProvidersServiceRegistry.V1
) -> ProvidersModels.V1.Output.SignIn:
    """How a sign-in stands: "wait", "ok" or "error". 404 for a state not started here."""
    return await service.get_sign_in(state)
