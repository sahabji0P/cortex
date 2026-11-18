from functools import cache
from typing import Annotated

from fastapi import Depends

from app.entities import EntitiesRegistry
from app.entities.gem_ent import GemConfig, GemEntityClass
from app.routers.gems.models import GemsModels


class GemsServiceV1Class:
    def __init__(self, gems: GemEntityClass | None = None) -> None:
        # Tests hand in their own. The app uses the one the registry made at startup.
        self.gems = EntitiesRegistry.Gems if gems is None else gems

    @staticmethod
    def _gem_out(gem: GemConfig) -> GemsModels.V1.Output.Gem:
        return GemsModels.V1.Output.Gem(
            id=str(gem.id),
            version=gem.version,
            name=gem.name,
            instructions=gem.instructions,
            model=gem.model,
            speaks_first=gem.speaks_first,
            tools=gem.tools,
            created_at=gem.created_at,
            updated_at=gem.updated_at,
        )

    async def create_gem(self, payload: GemsModels.V1.Input.CreateGem) -> GemsModels.V1.Output.Gem:
        gem = await self.gems.create(
            name=payload.name,
            instructions=payload.instructions,
            model=payload.model,
            speaks_first=payload.speaks_first,
            tools=payload.tools,
        )
        return self._gem_out(gem)

    async def list_gems(self) -> list[GemsModels.V1.Output.Gem]:
        return [self._gem_out(gem) for gem in await self.gems.list_all()]

    async def get_gem(self, gem_id: str) -> GemsModels.V1.Output.Gem:
        return self._gem_out(await self.gems.get(gem_id))

    async def update_gem(
        self,
        gem_id: str,
        payload: GemsModels.V1.Input.UpdateGem,
    ) -> GemsModels.V1.Output.Gem:
        return self._gem_out(await self.gems.update(gem_id, payload.changes()))

    async def delete_gem(self, gem_id: str) -> None:
        await self.gems.delete(gem_id)


# Cached, so every request gets the one service.
@cache
def get_gems_service_v1() -> GemsServiceV1Class:
    return GemsServiceV1Class()


class GemsServiceRegistry:
    type V1 = Annotated[GemsServiceV1Class, Depends(get_gems_service_v1)]
