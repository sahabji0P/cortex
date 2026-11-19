from functools import cache
from typing import Annotated

from fastapi import Depends

from app.core.errors import ConflictError
from app.entities import EntitiesRegistry
from app.entities.providers_ent import OpenAIRoute, OpenAIWayNotSetUpError, ProviderEntityClass
from app.routers.providers.models import ProvidersModels


class ProvidersServiceV1Class:
    def __init__(self, providers: ProviderEntityClass | None = None) -> None:
        # Tests hand in their own. The app uses the one the registry made at startup.
        self.providers = EntitiesRegistry.Providers if providers is None else providers

    @staticmethod
    def _route_out(route: OpenAIRoute) -> ProvidersModels.V1.Output.OpenAIRoute:
        return ProvidersModels.V1.Output.OpenAIRoute(
            via=route.via, api=route.api, proxy=route.proxy
        )

    async def get_openai(self) -> ProvidersModels.V1.Output.OpenAIRoute:
        return self._route_out(self.providers.openai_route())

    async def set_openai(
        self, payload: ProvidersModels.V1.Input.SetOpenAIRoute
    ) -> ProvidersModels.V1.Output.OpenAIRoute:
        try:
            return self._route_out(await self.providers.set_openai_via(payload.via))
        except OpenAIWayNotSetUpError as error:
            raise ConflictError(str(error)) from error


# Cached, so every request gets the one service.
@cache
def get_providers_service_v1() -> ProvidersServiceV1Class:
    return ProvidersServiceV1Class()


class ProvidersServiceRegistry:
    type V1 = Annotated[ProvidersServiceV1Class, Depends(get_providers_service_v1)]
