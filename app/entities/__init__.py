from app.adapters import AdapterRegistry
from app.entities.user_ent import User, UserEntityClass


class EntitiesRegistry:
    @classmethod
    async def initialize(cls, adapter: AdapterRegistry) -> None:
        await adapter.DocDB.create_index(User, [("email", 1)], unique=True)
        cls.Users = UserEntityClass(
            docdb=adapter.DocDB,
            cache=adapter.Cache,
        )


__all__ = ["EntitiesRegistry"]
