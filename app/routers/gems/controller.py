from fastapi import APIRouter, Response

from app.routers.gems.models import GemsModels
from app.routers.gems.service import GemsServiceRegistry

# No sign-in, like the voice routes: this backend has one user for now.
router = APIRouter(prefix="/v1/gems", tags=["gems"])


@router.post("", status_code=201)
async def create_gem(
    payload: GemsModels.V1.Input.CreateGem,
    service: GemsServiceRegistry.V1,
) -> GemsModels.V1.Output.Gem:
    return await service.create_gem(payload)


@router.get("")
async def list_gems(service: GemsServiceRegistry.V1) -> list[GemsModels.V1.Output.Gem]:
    """Every gem, in the order they were made."""
    return await service.list_gems()


@router.get("/{gem_id}")
async def get_gem(gem_id: str, service: GemsServiceRegistry.V1) -> GemsModels.V1.Output.Gem:
    return await service.get_gem(gem_id)


@router.get("/{gem_id}/chat")
async def get_chat(gem_id: str, service: GemsServiceRegistry.V1) -> GemsModels.V1.Output.Chat:
    """The gem's conversation so far, calls included, oldest first, for showing on screen."""
    return await service.get_chat(gem_id)


@router.patch("/{gem_id}")
async def update_gem(
    gem_id: str,
    payload: GemsModels.V1.Input.UpdateGem,
    service: GemsServiceRegistry.V1,
) -> GemsModels.V1.Output.Gem:
    return await service.update_gem(gem_id, payload)


@router.delete("/{gem_id}", status_code=204)
async def delete_gem(gem_id: str, service: GemsServiceRegistry.V1) -> Response:
    await service.delete_gem(gem_id)
    return Response(status_code=204)
