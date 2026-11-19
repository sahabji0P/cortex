from functools import cache
from typing import Annotated

from fastapi import Depends

from app.core.errors import AppError, ConflictError, NotFoundError
from app.entities import EntitiesRegistry
from app.entities.providers_ent import (
    OpenAIRoute,
    OpenAIWayNotSetUpError,
    ProviderEntityClass,
    ProxyNotManageableError,
    ProxyUnreachableError,
)
from app.routers.providers.models import ProvidersModels


class ProvidersServiceV1Class:
    def __init__(self, providers: ProviderEntityClass | None = None) -> None:
        # Tests hand in their own. The app uses the one the registry made at startup.
        self.providers = EntitiesRegistry.Providers if providers is None else providers

    @staticmethod
    def _route_out(route: OpenAIRoute) -> ProvidersModels.V1.Output.OpenAIRoute:
        return ProvidersModels.V1.Output.OpenAIRoute(
            via=route.via,
            api=route.api,
            proxy=route.proxy,
            manageable=route.manageable,
            accounts=None
            if route.accounts is None
            else [
                ProvidersModels.V1.Output.Account(
                    email=login.email, plan=login.plan, active=login.active, problem=login.problem
                )
                for login in route.accounts
            ],
        )

    async def get_openai(self) -> ProvidersModels.V1.Output.OpenAIRoute:
        return self._route_out(await self.providers.openai_route())

    async def set_openai(
        self, payload: ProvidersModels.V1.Input.SetOpenAIRoute
    ) -> ProvidersModels.V1.Output.OpenAIRoute:
        try:
            return self._route_out(await self.providers.set_openai_via(payload.via))
        except OpenAIWayNotSetUpError as error:
            raise ConflictError(str(error)) from error

    async def start_sign_in(self) -> ProvidersModels.V1.Output.SignInStarted:
        try:
            url, state = await self.providers.start_sign_in()
        except ProxyNotManageableError as error:
            raise ConflictError(str(error)) from error
        except ProxyUnreachableError as error:
            raise AppError(502, "proxy_unreachable", str(error)) from error
        return ProvidersModels.V1.Output.SignInStarted(url=url, state=state)

    async def get_sign_in(self, state: str) -> ProvidersModels.V1.Output.SignIn:
        sign_in = self.providers.sign_in(state)
        if sign_in is None:
            raise NotFoundError("sign-in")
        return ProvidersModels.V1.Output.SignIn(
            status=sign_in.status, reason=sign_in.reason, email=sign_in.email
        )


# Cached, so every request gets the one service.
@cache
def get_providers_service_v1() -> ProvidersServiceV1Class:
    return ProvidersServiceV1Class()


class ProvidersServiceRegistry:
    type V1 = Annotated[ProvidersServiceV1Class, Depends(get_providers_service_v1)]
