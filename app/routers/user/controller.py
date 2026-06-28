from typing import Annotated

import jwt
from fastapi import APIRouter, Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.errors import UnauthorizedError
from app.core.security import decode_access_token
from app.routers.user.models import UserModels
from app.routers.user.service import UserServiceRegistry

router = APIRouter(prefix="/users", tags=["users"])

_bearer = HTTPBearer(auto_error=False)


async def _get_current_user_id(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> str:
    if not credentials:
        raise UnauthorizedError()
    try:
        return decode_access_token(credentials.credentials)
    except jwt.InvalidTokenError as err:
        raise UnauthorizedError("Invalid token") from err


@router.post("/register", response_model=UserModels.V1.Output.User, status_code=201)
async def register(
    payload: UserModels.V1.Input.Register,
    service: UserServiceRegistry.V1,
) -> UserModels.V1.Output.User:
    return await service.register(payload)


@router.post("/login", response_model=UserModels.V1.Output.Token)
async def login(
    payload: UserModels.V1.Input.Login,
    service: UserServiceRegistry.V1,
) -> UserModels.V1.Output.Token:
    return await service.login(payload)


@router.get("/me", response_model=UserModels.V1.Output.User)
async def me(
    service: UserServiceRegistry.V1,
    user_id: Annotated[str, Depends(_get_current_user_id)],
) -> UserModels.V1.Output.User:
    return await service.get_me(user_id)
