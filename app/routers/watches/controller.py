from fastapi import APIRouter, Response

from app.routers.watches.models import WatchesModels
from app.routers.watches.service import WatchesServiceRegistry

# No sign-in, like the gems routes: this backend has one user for now.
router = APIRouter(prefix="/v1/gems", tags=["watches"])


@router.get("/{gem_id}/watches")
async def list_watches(
    gem_id: str, service: WatchesServiceRegistry.V1
) -> list[WatchesModels.V1.Output.Watch]:
    """The gem's watches, the newest first. The gem makes them; the user can remove one."""
    return await service.list_watches(gem_id)


@router.get("/{gem_id}/watches/{watch_id}/hook")
async def get_hook(
    gem_id: str, watch_id: str, response: Response, service: WatchesServiceRegistry.V1
) -> WatchesModels.V1.Output.Hook:
    """A plain webhook watch's address and secret, which the app asks for when the user does.

    Never in the list, nor in a frame. A timer, or a watch on MCP events, has none: a 404.
    """
    hook = await service.hook(gem_id, watch_id)
    # It holds a secret: not to be kept by anyone on the way.
    response.headers["Cache-Control"] = "no-store"
    return hook


@router.delete("/{gem_id}/watches/{watch_id}", status_code=204)
async def remove_watch(gem_id: str, watch_id: str, service: WatchesServiceRegistry.V1) -> Response:
    """Remove one of the gem's watches: it stops at once, and the gem's log says the user did.

    The gem is told too (`watch.ended`), so it stops counting on it.
    """
    await service.remove_watch(gem_id, watch_id)
    return Response(status_code=204)
