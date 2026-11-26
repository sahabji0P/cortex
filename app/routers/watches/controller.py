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


@router.delete("/{gem_id}/watches/{watch_id}", status_code=204)
async def remove_watch(gem_id: str, watch_id: str, service: WatchesServiceRegistry.V1) -> Response:
    """Remove one of the gem's watches: it stops at once, and the gem's log says the user did."""
    await service.remove_watch(gem_id, watch_id)
    return Response(status_code=204)
