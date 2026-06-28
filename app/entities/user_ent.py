from datetime import UTC, datetime

from bson import ObjectId
from bson.errors import InvalidId
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from pydantic_mongo import PydanticObjectId

from app.adapters.docdb_adp import DocDBAdapterClass
from app.adapters.redis_adp import RedisAdapterClass
from app.core.errors import ConflictError, NotFoundError, UnauthorizedError
from app.core.security import create_access_token, hash_password, verify_password

_SESSION_TTL = 60 * 60 * 24  # 24 hours


class User(BaseModel):
    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PydanticObjectId = Field(default_factory=ObjectId, alias="_id")
    email: EmailStr
    hashed_password: str
    is_active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class UserEntityClass:
    def __init__(self, docdb: DocDBAdapterClass, cache: RedisAdapterClass) -> None:
        self._docdb = docdb
        self._cache = cache

    async def get_by_id(self, user_id: str) -> User:
        try:
            object_id = ObjectId(user_id)
        except InvalidId as err:
            raise NotFoundError("User") from err
        user = await self._docdb.get(User, filters={"id": object_id})
        if not user:
            raise NotFoundError("User")
        return user

    async def get_by_email(self, email: str) -> User | None:
        return await self._docdb.get(User, filters={"email": email})

    async def register(self, email: str, password: str) -> User:
        if await self.get_by_email(email):
            raise ConflictError("Email already registered")
        user = User(email=email, hashed_password=hash_password(password))
        return await self._docdb.insert(user)

    async def authenticate(self, email: str, password: str) -> str:
        user = await self.get_by_email(email)
        if not user or not verify_password(password, user.hashed_password):
            raise UnauthorizedError("Invalid email or password")
        token = create_access_token(str(user.id))
        await self._cache.client.setex(f"session:{user.id}", _SESSION_TTL, token)
        return token

    async def revoke_session(self, user_id: str) -> None:
        await self._cache.client.delete(f"session:{user_id}")
