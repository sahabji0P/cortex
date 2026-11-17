"""Written chat with a gem: what it is told, what is stored, and the standing socket.

The expected instructions are written out here in full, not built from the module's
constants, so any change to what the model reads shows up as a failing test.
"""

from collections.abc import Iterable, MutableMapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

import pytest
from bson import ObjectId
from pydantic import BaseModel

from app.entities.chat_store import ChatItem, ChatStoreClass
from app.entities.gem_ent import GemConfig, GemEntityClass
from app.entities.instructions import instruction_layers, instructions

if TYPE_CHECKING:
    from app.adapters.docdb_adp import DocDBAdapterClass

_NOW = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)

_KUBER_IN_WRITING = (
    "The user keeps a few assistants of their own, which they call gems. Each gem has its "
    "own name and its own work. The place they share is called the Darbar.\n"
    "\n"
    "You are Kuber.\n"
    "\n"
    "You look after the user's personal finances.\n"
    "\n"
    "You are in a written conversation. The user reads what you write in a chat window.\n"
    "\n"
    "- Write the way a person writes a message: plainly, and only as long as the answer "
    "needs.\n"
    "- Write plain text, with no markdown or other formatting marks. The window shows your "
    "words exactly as you write them.\n"
    '- An earlier reply of yours that ends in "…" was cut off at that point, because the '
    "user sent a new message. They read nothing after it, so do not assume they know the "
    'rest. Never end a reply with "…" yourself.\n'
)
_WRITTEN_SEARCH_RULES = (
    "- When what you are about to do will keep the user waiting, such as looking something "
    "up, first say in a few words what you are going to do, the way a person would.\n"
    "- When you answer from search results, name the source.\n"
)
_TODAY = "\nToday is Tuesday, 17 November 2026.\n"


def _kuber(**fields: object) -> GemConfig:
    return GemConfig.model_validate(
        {
            "name": "Kuber",
            "instructions": "You look after the user's personal finances.",
            "created_at": _NOW,
            "updated_at": _NOW,
            **fields,
        }
    )


# ---- what a gem is told in writing


@pytest.mark.parametrize(
    ("search", "expected"),
    [
        (True, _KUBER_IN_WRITING + _WRITTEN_SEARCH_RULES + "- A need of the model's.\n" + _TODAY),
        (False, _KUBER_IN_WRITING + "- A need of the model's.\n" + _TODAY),
    ],
)
def test_a_gem_in_writing_is_told_the_written_rules(search: bool, expected: str) -> None:
    told = instructions(
        gem=_kuber(),
        search=search,
        written=True,
        model_needs="- A need of the model's.\n",
        now=_NOW,
    )
    assert told == expected


def test_the_written_layers_take_the_place_of_the_spoken_ones() -> None:
    layers = instruction_layers(gem=_kuber(), search=True, written=True, model_needs="", now=_NOW)
    assert [layer.name for layer in layers] == [
        "darbar",
        "gem",
        "written",
        "tools",
        "model",
        "date",
    ]
    told = "".join(layer.text for layer in layers)
    # Nothing of a call's: no speech, and no searches that carry on in the background.
    assert "spoken" not in told
    assert "cancel_tool_call" not in told


def test_writing_is_always_with_a_gem() -> None:
    with pytest.raises(ValueError, match="always with a gem"):
        instructions(gem=None, search=False, written=True, model_needs="")


# ---- the store


def _name(collection: type[BaseModel]) -> str:
    return getattr(collection, "__collection_name__", collection.__name__)


class _DocDB:
    """The database adapter, faked: documents as plain dicts, one list per collection.

    Reads go through `model_validate`, as the real adapter's do. Times lose their zone
    on the way in, as they do in Mongo. A conversation's (gem_id, place) is unique, as
    the real index makes it.
    """

    def __init__(self) -> None:
        self.collections: dict[str, list[dict[str, Any]]] = {}
        # When set, every write waits for it: a slow store.
        self.slow: Any = None

    def _all(self, collection: type[BaseModel]) -> list[dict[str, Any]]:
        return self.collections.setdefault(_name(collection), [])

    @staticmethod
    def _matches(document: dict[str, Any], filters: MutableMapping[str, Any]) -> bool:
        return all(
            document.get("_id" if key == "id" else key) == value for key, value in filters.items()
        )

    @staticmethod
    def _stored(data: BaseModel) -> dict[str, Any]:
        document = data.model_dump(by_alias=True)
        return {
            key: value.replace(tzinfo=None) if isinstance(value, datetime) else value
            for key, value in document.items()
        }

    async def get[T: BaseModel](
        self, collection: type[T], *, filters: MutableMapping[str, Any]
    ) -> T | None:
        found = [d for d in self._all(collection) if self._matches(d, filters)]
        return collection.model_validate(found[0]) if found else None

    async def get_many[T: BaseModel](
        self,
        collection: type[T],
        *,
        filters: MutableMapping[str, Any],
        sort: Iterable[tuple[str, Literal[-1, 1]]] | None = None,
    ) -> list[T]:
        found = [d for d in self._all(collection) if self._matches(d, filters)]
        for field, direction in reversed(list(sort or [])):
            found.sort(key=lambda document: document[field], reverse=direction == -1)
        return [collection.model_validate(document) for document in found]

    async def count(self, collection: type[BaseModel], *, filters: MutableMapping[str, Any]) -> int:
        return len([d for d in self._all(collection) if self._matches(d, filters)])

    async def insert[T: BaseModel](self, data: T) -> T:
        self._all(type(data)).append(self._stored(data))
        return data

    async def insert_many[T: BaseModel](self, data: Sequence[T]) -> list[T]:
        if self.slow is not None:
            await self.slow.wait()
        documents = self._all(type(data[0]))
        for item in data:
            document = self._stored(item)
            if isinstance(item, ChatItem) and any(
                (d["gem_id"], d["place"]) == (document["gem_id"], document["place"])
                for d in documents
            ):
                raise RuntimeError("duplicate key: that place is taken")
            documents.append(document)
        return list(data)

    async def update(
        self,
        collection: type[BaseModel],
        filters: MutableMapping[str, Any],
        update_data: MutableMapping[str, Any],
    ) -> int:
        found = [d for d in self._all(collection) if self._matches(d, filters)]
        for document in found[:1]:
            document.update(update_data)
        return len(found[:1])

    async def delete(self, collection: type[BaseModel], filters: MutableMapping[str, Any]) -> int:
        documents = self._all(collection)
        found = [d for d in documents if self._matches(d, filters)][:1]
        for document in found:
            documents.remove(document)
        return len(found)

    async def delete_many(
        self, collection: type[BaseModel], filters: MutableMapping[str, Any]
    ) -> int:
        documents = self._all(collection)
        kept = [d for d in documents if not self._matches(d, filters)]
        gone = len(documents) - len(kept)
        documents[:] = kept
        return gone


def _docdb() -> tuple[_DocDB, "DocDBAdapterClass"]:
    docdb = _DocDB()
    return docdb, cast("DocDBAdapterClass", docdb)


def _item(gem_id: ObjectId, place: int, text: str, **fields: Any) -> ChatItem:
    return ChatItem(
        gem_id=gem_id,
        place=place,
        at=_NOW,
        item={"role": "user", "content": text},
        **fields,
    )


async def test_a_conversation_is_read_back_in_order_with_its_times_in_utc() -> None:
    docdb, adapter = _docdb()
    store = ChatStoreClass(adapter)
    gem_id, other = ObjectId(), ObjectId()

    await store.add([_item(gem_id, 1, "second"), _item(gem_id, 2, "third")])
    await store.add([_item(other, 0, "another gem's")])
    await store.add([_item(gem_id, 0, "first")])

    read = await store.load(gem_id)
    assert [item.item for item in read] == [
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
        {"role": "user", "content": "third"},
    ]
    # Stored without a zone, as Mongo keeps them, and read back in UTC.
    assert docdb.collections["ChatItems"][0]["at"].tzinfo is None
    assert all(item.at == _NOW and item.at.tzinfo is UTC for item in read)


async def test_every_shape_of_item_is_stored_as_it_was_given() -> None:
    _, adapter = _docdb()
    store = ChatStoreClass(adapter)
    gem_id = ObjectId()
    items: list[Any] = [
        {"role": "user", "content": "weather?"},
        {"role": "assistant", "content": "Let me look."},
        {"type": "function_call", "call_id": "c1", "name": "web_search", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "Sunny."},
    ]

    await store.add(
        [
            ChatItem(gem_id=gem_id, place=place, at=_NOW, item=item, shown=None)
            for place, item in enumerate(items)
        ]
    )

    assert [item.item for item in await store.load(gem_id)] == items


async def test_deleting_a_gem_deletes_its_conversation() -> None:
    docdb, adapter = _docdb()
    gems, store = GemEntityClass(adapter), ChatStoreClass(adapter)
    gem, other = await gems.create(name="Kuber"), await gems.create(name="Lata")
    await store.add([_item(gem.id, 0, "hello"), _item(gem.id, 1, "again")])
    await store.add([_item(other.id, 0, "hello")])

    await gems.delete(str(gem.id))

    assert await store.load(gem.id) == []
    assert len(await store.load(other.id)) == 1
    assert len(docdb.collections["ChatItems"]) == 1
