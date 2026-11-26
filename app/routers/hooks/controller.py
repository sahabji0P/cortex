from fastapi import APIRouter, Request, Response

from app.entities.watch_ent import HOOKS_PATH
from app.routers.hooks.service import HooksServiceRegistry

# The one route meant to be reached from outside this Mac, through a tunnel to
# `PUBLIC_URL`. No sign-in: the token in the address is what lets a post in.
router = APIRouter(prefix=HOOKS_PATH, tags=["hooks"])


@router.post("/{token}")
async def hook(token: str, request: Request, service: HooksServiceRegistry.V1) -> Response:
    """A post to a watch's webhook address, such as a GitHub webhook's: it wakes the gem.

    202 when it is handed over. 200 for a GitHub `ping`, which wakes nobody. 404 for an
    address that is no watch's, 401 for a post whose signature is not the watch's, or
    that is not signed when the watch has taken a signed one, 413 for one over 1 MB, 409
    for a watch that is paused, 503 when it could not be kept. Every answer is empty.
    """
    return await service.hook(token, request)
