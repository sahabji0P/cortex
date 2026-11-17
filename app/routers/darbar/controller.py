from fastapi import APIRouter

from app.routers.darbar.models import DarbarModels
from app.routers.darbar.service import DarbarServiceRegistry

# No sign-in, like the gems routes: this backend has one user for now.
router = APIRouter(prefix="/v1/darbar", tags=["darbar"])


@router.get("")
async def get_darbar(service: DarbarServiceRegistry.V1) -> DarbarModels.V1.Output.Darbar:
    """The Darbar text every gem is told first, and whether it is the built-in one."""
    return await service.get_darbar()


@router.put("")
async def set_darbar(
    payload: DarbarModels.V1.Input.SetDarbar,
    service: DarbarServiceRegistry.V1,
) -> DarbarModels.V1.Output.Darbar:
    """Replace it. Calls already open keep the text they opened with."""
    return await service.set_darbar(payload)


@router.delete("")
async def reset_darbar(service: DarbarServiceRegistry.V1) -> DarbarModels.V1.Output.Darbar:
    """Go back to the built-in text. Answers like `GET`, not with an empty 204."""
    return await service.reset_darbar()
