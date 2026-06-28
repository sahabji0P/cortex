from redis.asyncio import Redis, from_url

from app.config import settings


class RedisAdapterClass:
    def __init__(self) -> None:
        self._client: Redis | None = None

    async def initialize(self) -> None:
        # await eagerly initializes the connection so failures surface at startup.
        self._client = await from_url(settings.redis_url, decode_responses=True)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    @property
    def client(self) -> Redis:
        if self._client is None:
            raise RuntimeError("Redis not initialized")
        return self._client
