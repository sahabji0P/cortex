from collections.abc import Iterable, MutableMapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

import pytest
from bson import ObjectId
from fastapi.routing import APIRoute
from pydantic import BaseModel, ValidationError

from app.core.errors import NotFoundError
from app.entities.gem_ent import (
    GEM_CONFIG_VERSION,
    GemConfig,
    GemEntityClass,
    GemVersionError,
    upgrade_gem_document,
)
from app.routers.gems.controller import router
from app.routers.gems.models import GemsModels
from app.routers.gems.service import GemsServiceV1Class

if TYPE_CHECKING:
    from app.adapters.docdb_adp import DocDBAdapterClass


class _DocDBFake:
    """Keeps documents as plain dicts and reads them back through `model_validate`.

    That is what the real adapter does, so a read here goes through the same
    versioning a read from Mongo does.
    """

    def __init__(self) -> None:
        self.documents: dict[ObjectId, dict[str, Any]] = {}

    @staticmethod
    def _id(filters: MutableMapping[str, Any]) -> ObjectId:
        assert set(filters) == {"id"}, filters
        return filters["id"]

    async def get[T: BaseModel](
        self, collection: type[T], *, filters: MutableMapping[str, Any]
    ) -> T | None:
        document = self.documents.get(self._id(filters))
        return None if document is None else collection.model_validate(document)

    async def get_many[T: BaseModel](
        self,
        collection: type[T],
        *,
        filters: MutableMapping[str, Any],
        sort: Iterable[tuple[str, Literal[-1, 1]]] | None = None,
    ) -> list[T]:
        assert filters == {}
        documents = list(self.documents.values())
        for field, direction in reversed(list(sort or [])):
            documents.sort(key=lambda document: document[field], reverse=direction == -1)
        return [collection.model_validate(document) for document in documents]

    async def insert[T: BaseModel](self, data: T) -> T:
        document = data.model_dump(by_alias=True)
        self.documents[document["_id"]] = document
        return data

    async def update(
        self,
        _collection: type[BaseModel],
        filters: MutableMapping[str, Any],
        update_data: MutableMapping[str, Any],
    ) -> int:
        document = self.documents.get(self._id(filters))
        if document is None:
            return 0
        document.update(update_data)
        return 1

    async def delete(self, _collection: type[BaseModel], filters: MutableMapping[str, Any]) -> int:
        return 1 if self.documents.pop(self._id(filters), None) else 0


def _gems() -> tuple[GemEntityClass, _DocDBFake]:
    docdb = _DocDBFake()
    return GemEntityClass(cast("DocDBAdapterClass", docdb)), docdb


def _stored(**fields: Any) -> dict[str, Any]:
    """A config as it would sit in the database."""
    now = datetime(2026, 11, 17, tzinfo=UTC)
    return {
        "_id": ObjectId(),
        "name": "Lata",
        "instructions": "",
        "model": None,
        "speaks_first": False,
        "tools": ["web_search"],
        "created_at": now,
        "updated_at": now,
        **fields,
    }


async def test_a_gem_is_stored_and_read_back_as_it_was_made() -> None:
    gems, docdb = _gems()

    made = await gems.create(
        name="  Lata  ",
        instructions="A patient tutor.",
        model="Sonnet 5.5",
        speaks_first=True,
        tools=[],
    )
    read = await gems.get(str(made.id))

    assert read == made
    assert read.name == "Lata"
    assert read.model == "Sonnet 5.5"
    assert read.speaks_first is True
    assert read.tools == []
    assert docdb.documents[made.id]["version"] == GEM_CONFIG_VERSION


async def test_a_new_gem_has_the_defaults() -> None:
    gems, _ = _gems()

    gem = await gems.create(name="Lata")

    assert gem.version == GEM_CONFIG_VERSION
    assert gem.instructions == ""
    assert gem.model is None
    assert gem.speaks_first is False
    assert gem.tools == ["web_search"]
    assert gem.created_at == gem.updated_at


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
async def test_a_gem_needs_a_name_that_is_not_blank_or_too_long(name: str) -> None:
    gems, docdb = _gems()

    with pytest.raises(ValidationError):
        await gems.create(name=name)
    assert docdb.documents == {}


async def test_a_tool_cannot_be_named_twice() -> None:
    gems, _ = _gems()

    with pytest.raises(ValidationError, match="more than once"):
        await gems.create(name="Lata", tools=["web_search", " web_search"])


async def test_a_gem_cannot_be_answered_by_a_model_that_does_not_exist() -> None:
    with pytest.raises(ValidationError):
        GemConfig.model_validate(_stored(model="GPT-2"))


async def test_an_update_changes_only_the_fields_given() -> None:
    gems, docdb = _gems()
    made = await gems.create(name="Lata", instructions="A patient tutor.", model="Opus 5.5")

    changed = await gems.update(str(made.id), {"name": " Asha ", "model": None})

    assert changed.name == "Asha"
    assert changed.model is None
    assert changed.instructions == "A patient tutor."
    assert changed.tools == ["web_search"]
    assert changed.created_at == made.created_at
    assert changed.updated_at > made.updated_at
    assert await gems.get(str(made.id)) == changed
    assert docdb.documents[made.id]["name"] == "Asha"


async def test_an_update_is_checked_like_a_new_gem() -> None:
    gems, _ = _gems()
    made = await gems.create(name="Lata")

    with pytest.raises(ValidationError):
        await gems.update(str(made.id), {"name": "  "})
    with pytest.raises(ValidationError):
        await gems.update(str(made.id), {"name": None})
    with pytest.raises(ValueError, match="cannot be changed"):
        await gems.update(str(made.id), {"created_at": datetime.now(UTC)})
    assert (await gems.get(str(made.id))).name == "Lata"


async def test_gems_are_listed_with_the_latest_change_first() -> None:
    gems, docdb = _gems()
    for name, day in (("Old", 1), ("Newest", 3), ("Middle", 2)):
        document = _stored(name=name, updated_at=datetime(2026, 11, day, tzinfo=UTC))
        docdb.documents[document["_id"]] = document

    assert [gem.name for gem in await gems.list_all()] == ["Newest", "Middle", "Old"]


async def test_a_deleted_gem_is_gone() -> None:
    gems, _ = _gems()
    made = await gems.create(name="Lata")

    await gems.delete(str(made.id))

    with pytest.raises(NotFoundError):
        await gems.get(str(made.id))
    with pytest.raises(NotFoundError):
        await gems.delete(str(made.id))


@pytest.mark.parametrize("gem_id", [str(ObjectId()), "not-an-id", ""])
async def test_a_missing_or_malformed_id_is_not_found(gem_id: str) -> None:
    gems, _ = _gems()

    with pytest.raises(NotFoundError):
        await gems.get(gem_id)
    with pytest.raises(NotFoundError):
        await gems.update(gem_id, {"name": "Asha"})
    with pytest.raises(NotFoundError):
        await gems.delete(gem_id)


def test_a_config_in_the_current_version_is_read_as_it_is() -> None:
    document = _stored(version=GEM_CONFIG_VERSION)

    assert upgrade_gem_document(document) == document


def test_a_config_from_a_newer_version_is_refused() -> None:
    document = _stored(version=GEM_CONFIG_VERSION + 1, looks="blue")

    with pytest.raises(GemVersionError, match="newer"):
        upgrade_gem_document(document)
    with pytest.raises(ValidationError, match="newer"):
        GemConfig.model_validate(document)


@pytest.mark.parametrize("version", [0, -1, "1", True, None])
def test_a_config_whose_version_is_not_a_version_is_refused(version: object) -> None:
    with pytest.raises(GemVersionError):
        upgrade_gem_document(_stored(version=version))


def test_a_config_without_a_version_is_read_as_version_1() -> None:
    # Version 1 is the first shape, so a config with no version cannot be older.
    # Reading it as version 1 lets a config written by hand leave the field out.
    document = _stored()
    assert "version" not in document

    assert upgrade_gem_document(document)["version"] == 1
    assert GemConfig.model_validate(document).version == 1


def _service() -> GemsServiceV1Class:
    return GemsServiceV1Class(_gems()[0])


def test_the_routes_are_the_five_a_client_expects() -> None:
    routes = {
        (method, route.path, route.status_code)
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }

    assert routes == {
        ("POST", "/v1/gems", 201),
        ("GET", "/v1/gems", None),
        ("GET", "/v1/gems/{gem_id}", None),
        ("PATCH", "/v1/gems/{gem_id}", None),
        ("DELETE", "/v1/gems/{gem_id}", 204),
    }


async def test_the_service_hands_out_a_gem_with_its_id_as_text_and_its_version() -> None:
    service = _service()

    made = await service.create_gem(GemsModels.V1.Input.CreateGem(name="Lata", model="Haiku 5.5"))
    read = await service.get_gem(made.id)

    assert read == made
    assert ObjectId(made.id)
    assert made.version == GEM_CONFIG_VERSION
    assert made.tools == ["web_search"]
    assert [gem.id for gem in await service.list_gems()] == [made.id]


async def test_the_service_changes_only_what_a_patch_sends() -> None:
    service = _service()
    made = await service.create_gem(
        GemsModels.V1.Input.CreateGem(name="Lata", model="Opus 5.5", speaks_first=True)
    )

    patch = GemsModels.V1.Input.UpdateGem.model_validate({"model": None})
    changed = await service.update_gem(made.id, patch)

    assert changed.model is None
    assert changed.name == "Lata"
    assert changed.speaks_first is True


async def test_the_service_deletes_and_then_cannot_find_a_gem() -> None:
    service = _service()
    made = await service.create_gem(GemsModels.V1.Input.CreateGem(name="Lata"))

    await service.delete_gem(made.id)

    with pytest.raises(NotFoundError):
        await service.get_gem(made.id)
    with pytest.raises(NotFoundError):
        await service.get_gem("not-an-id")


@pytest.mark.parametrize(
    "body",
    [
        {"name": "  "},
        {"name": "Lata", "model": "GPT-2"},
        {"name": "Lata", "tools": ["web_search", "web_search"]},
        {"name": "Lata", "instructions": "x" * 20_001},
    ],
)
def test_a_new_gem_that_breaks_a_rule_is_refused(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        GemsModels.V1.Input.CreateGem.model_validate(body)


@pytest.mark.parametrize("field", ["name", "instructions", "speaks_first", "tools"])
def test_a_patch_cannot_set_a_field_other_than_the_model_to_null(field: str) -> None:
    with pytest.raises(ValidationError, match="cannot be null"):
        GemsModels.V1.Input.UpdateGem.model_validate({field: None})


def test_a_patch_keeps_only_the_fields_sent_and_a_null_model() -> None:
    patch = GemsModels.V1.Input.UpdateGem.model_validate({"name": " Asha ", "model": None})

    assert patch.changes() == {"name": "Asha", "model": None}
    assert GemsModels.V1.Input.UpdateGem().changes() == {}
