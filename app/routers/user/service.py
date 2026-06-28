from typing import Annotated

from fastapi import Depends

from app.entities import EntitiesRegistry
from app.entities.user_ent import User
from app.routers.user.models import UserModels


class UserServiceV1Class:
    def __init__(self) -> None:
        self.users = EntitiesRegistry.Users

    def _to_out(self, user: User) -> UserModels.V1.Output.User:
        return UserModels.V1.Output.User(
            id=str(user.id),
            email=user.email,
            is_active=user.is_active,
            created_at=user.created_at,
        )

    async def register(self, payload: UserModels.V1.Input.Register) -> UserModels.V1.Output.User:
        user = await self.users.register(payload.email, payload.password)
        return self._to_out(user)

    async def login(self, payload: UserModels.V1.Input.Login) -> UserModels.V1.Output.Token:
        token = await self.users.authenticate(payload.email, payload.password)
        return UserModels.V1.Output.Token(access_token=token)

    async def get_me(self, user_id: str) -> UserModels.V1.Output.User:
        user = await self.users.get_by_id(user_id)
        return self._to_out(user)


_singleton: UserServiceV1Class | None = None


def get_user_service_v1() -> UserServiceV1Class:
    global _singleton  # noqa: PLW0603
    if _singleton is None:
        _singleton = UserServiceV1Class()
    return _singleton


class UserServiceRegistry:
    type V1 = Annotated[UserServiceV1Class, Depends(get_user_service_v1)]
