from functools import cache
from typing import Annotated

from fastapi import Depends

from app.entities import EntitiesRegistry
from app.entities.darbar_ent import Darbar, DarbarEntityClass
from app.routers.darbar.models import DarbarModels


class DarbarServiceV1Class:
    def __init__(self, darbar: DarbarEntityClass | None = None) -> None:
        # Tests hand in their own. The app uses the one the registry made at startup.
        self.darbar = EntitiesRegistry.Darbar if darbar is None else darbar

    @staticmethod
    def _darbar_out(darbar: Darbar) -> DarbarModels.V1.Output.Darbar:
        return DarbarModels.V1.Output.Darbar(text=darbar.text, is_default=darbar.is_default)

    async def get_darbar(self) -> DarbarModels.V1.Output.Darbar:
        return self._darbar_out(await self.darbar.read())

    async def set_darbar(
        self, payload: DarbarModels.V1.Input.SetDarbar
    ) -> DarbarModels.V1.Output.Darbar:
        return self._darbar_out(await self.darbar.set(payload.text))

    async def reset_darbar(self) -> DarbarModels.V1.Output.Darbar:
        return self._darbar_out(await self.darbar.reset())


# Cached, so every request gets the one service.
@cache
def get_darbar_service_v1() -> DarbarServiceV1Class:
    return DarbarServiceV1Class()


class DarbarServiceRegistry:
    type V1 = Annotated[DarbarServiceV1Class, Depends(get_darbar_service_v1)]
