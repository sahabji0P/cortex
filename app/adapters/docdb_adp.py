from collections.abc import Iterable, MutableMapping
from typing import Any, Literal, TypeVar

from pydantic import BaseModel
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from app.config import settings

_T = TypeVar("_T", bound=BaseModel)

type _UpdateOperator = Literal["set", "unset", "inc", "push", "pull"]


def _translate_id(filters: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    if "id" in filters:
        filters["_id"] = filters.pop("id")
    return filters


class DocDBAdapterClass:
    def __init__(self) -> None:
        self._client: AsyncMongoClient[Any] = AsyncMongoClient(
            settings.mongodb_url, uuidRepresentation="standard"
        )
        self._db: AsyncDatabase[Any] | None = None

    async def initialize(self) -> None:
        await self._client.aconnect()
        self._db = self._client.get_database(settings.mongodb_db_name)

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def db(self) -> AsyncDatabase[Any]:
        if self._db is None:
            raise RuntimeError("DocDB not initialized")
        return self._db

    async def get(self, collection: type[_T], *, filters: MutableMapping[str, Any]) -> _T | None:
        doc = await self.db.get_collection(collection.__name__).find_one(_translate_id(filters))
        if doc is None:
            return None
        return collection.model_validate(doc)

    async def get_many(
        self,
        collection: type[_T],
        *,
        filters: MutableMapping[str, Any],
        sort: Iterable[tuple[str, Literal[-1, 1]]] | None = None,
        limit: int | None = None,
    ) -> list[_T]:
        cursor = self.db.get_collection(collection.__name__).find(_translate_id(filters))
        if sort:
            cursor = cursor.sort([("_id" if f == "id" else f, d) for f, d in sort])
        if limit is not None and limit > 0:
            cursor = cursor.limit(limit)
        docs = await cursor.to_list(length=None)
        return [collection.model_validate(doc) for doc in docs]

    async def insert(self, data: _T) -> _T:
        await self.db.get_collection(data.__class__.__name__).insert_one(
            data.model_dump(by_alias=True)
        )
        return data

    async def update(
        self,
        collection: type[_T],
        filters: MutableMapping[str, Any],
        update_data: MutableMapping[str, Any],
        operator: _UpdateOperator = "set",
        *,
        upsert: bool = False,
    ) -> None:
        await self.db.get_collection(collection.__name__).update_one(
            _translate_id(filters), {f"${operator}": update_data}, upsert=upsert
        )

    async def delete_many(
        self, collection: type[_T], filters: MutableMapping[str, Any]
    ) -> int:
        result = await self.db.get_collection(collection.__name__).delete_many(
            _translate_id(filters)
        )
        return result.deleted_count

    async def count(self, collection: type[_T], *, filters: MutableMapping[str, Any]) -> int:
        return await self.db.get_collection(collection.__name__).count_documents(
            _translate_id(filters)
        )

    async def create_index(
        self,
        collection: type[_T],
        keys: list[tuple[str, Literal[-1, 1]]],
        *,
        unique: bool = False,
        name: str | None = None,
    ) -> str:
        return await self.db.get_collection(collection.__name__).create_index(
            keys, unique=unique, name=name
        )
