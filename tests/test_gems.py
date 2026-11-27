from collections.abc import Iterable, MutableMapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

import pytest
from bson import ObjectId
from fastapi.routing import APIRoute
from pydantic import BaseModel, ValidationError

from app.core.errors import NotFoundError
from app.entities.gem_ent import (
    DEFAULT_GEM_INSTRUCTIONS,
    GEM_CONFIG_VERSION,
    GemConfig,
    GemEntityClass,
    GemVersionError,
    upgrade_gem_document,
)
from app.entities.tools import GEM_TOOL_NAMES, web_search_tool
from app.routers.gems.controller import router
from app.routers.gems.models import GemsModels
from app.routers.gems.service import GemsServiceV1Class

if TYPE_CHECKING:
    from app.adapters.docdb_adp import DocDBAdapterClass
    from app.adapters.parallel_adp import ParallelAdapterClass


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

    async def count(
        self, _collection: type[BaseModel], *, filters: MutableMapping[str, Any]
    ) -> int:
        return int(self._id(filters) in self.documents)

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

    async def delete_many(
        self, _collection: type[BaseModel], _filters: MutableMapping[str, Any]
    ) -> int:
        # Only a gem's written conversation is deleted this way, and this fake keeps
        # none. Those are tested in `test_chat.py`.
        return 0


def _gems() -> tuple[GemEntityClass, _DocDBFake]:
    docdb = _DocDBFake()
    return GemEntityClass(cast("DocDBAdapterClass", docdb)), docdb


def _stored(**fields: Any) -> dict[str, Any]:
    """A config as it would sit in the database, in the current version unless told."""
    now = datetime(2026, 11, 17, tzinfo=UTC)
    return {
        "_id": ObjectId(),
        "version": GEM_CONFIG_VERSION,
        "name": "Gem Two",
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
        name="  Gem Two  ",
        instructions="A patient tutor.",
        model="Sonnet 5.5",
        speaks_first=True,
        tools=[],
    )
    read = await gems.get(str(made.id))

    assert read == made
    assert read.name == "Gem Two"
    assert read.model == "Sonnet 5.5"
    assert read.speaks_first is True
    assert read.tools == []
    assert docdb.documents[made.id]["version"] == GEM_CONFIG_VERSION


async def test_a_new_gem_has_the_defaults() -> None:
    gems, _ = _gems()

    gem = await gems.create(name="Gem Two")

    assert gem.version == GEM_CONFIG_VERSION
    assert gem.instructions == DEFAULT_GEM_INSTRUCTIONS
    assert gem.model is None
    assert gem.speaks_first is False
    assert gem.tools == ["web_search"]
    assert gem.compaction == "before_expiry"
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
        await gems.create(name="Gem Two", tools=["web_search", " web_search"])


async def test_a_gem_can_only_be_given_a_tool_there_is() -> None:
    gems, docdb = _gems()

    with pytest.raises(ValidationError, match="cannot be given these tools: weather"):
        await gems.create(name="Gem Two", tools=["web_search", "weather"])
    assert docdb.documents == {}

    made = await gems.create(name="Gem Two", tools=[" web_search "])
    assert made.tools == ["web_search"]
    with pytest.raises(ValidationError, match="cannot be given these tools: weather"):
        await gems.update(str(made.id), {"tools": ["weather"]})
    assert (await gems.get(str(made.id))).tools == ["web_search"]


def test_every_tool_a_gem_can_be_given_is_a_tool_there_is() -> None:
    search = web_search_tool(cast("ParallelAdapterClass", object()))

    assert (search.name,) == GEM_TOOL_NAMES


async def test_a_time_the_database_hands_back_without_a_zone_is_read_as_utc() -> None:
    # The database keeps no zone. A client sent a time without one cannot read it.
    gems, docdb = _gems()
    document = _stored(
        created_at=datetime(2026, 11, 17, 8, 0), updated_at=datetime(2026, 11, 17, 9, 30)
    )
    docdb.documents[document["_id"]] = document

    read = await gems.get(str(document["_id"]))
    (listed,) = await gems.list_all()

    assert read.created_at == listed.created_at == datetime(2026, 11, 17, 8, 0, tzinfo=UTC)
    assert read.updated_at == datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
    assert '"created_at":"2026-11-17T08:00:00Z"' in read.model_dump_json()


async def test_a_stored_gem_with_a_tool_that_has_gone_still_loads_and_can_be_changed() -> None:
    # The tool is only left out when a call opens. The config keeps what it was given.
    gems, docdb = _gems()
    document = _stored(tools=["web_search", "weather"])
    docdb.documents[document["_id"]] = document

    read = await gems.get(str(document["_id"]))
    changed = await gems.update(str(document["_id"]), {"name": "Gem Three"})

    assert read.tools == ["web_search", "weather"]
    assert changed.name == "Gem Three"
    assert changed.tools == ["web_search", "weather"]


async def test_a_gem_cannot_be_given_a_model_that_does_not_exist() -> None:
    gems, docdb = _gems()

    with pytest.raises(ValidationError):
        await gems.create(name="Gem Two", model=cast("Any", "GPT-2"))
    assert docdb.documents == {}

    made = await gems.create(name="Gem Two", model="Opus 5.5")
    with pytest.raises(ValidationError):
        await gems.update(str(made.id), {"model": "GPT-2"})
    assert (await gems.get(str(made.id))).model == "Opus 5.5"


async def test_a_stored_gem_whose_model_has_gone_is_answered_by_the_default() -> None:
    # One gem saved before a model was dropped must not stop every gem being listed.
    gems, docdb = _gems()
    document = _stored(model="GPT-2")
    docdb.documents[document["_id"]] = document

    read = await gems.get(str(document["_id"]))
    listed = await gems.list_all()

    assert read.model is None
    assert [gem.model for gem in listed] == [None]


async def test_an_update_changes_only_the_fields_given() -> None:
    gems, docdb = _gems()
    made = await gems.create(name="Gem Two", instructions="A patient tutor.", model="Opus 5.5")

    changed = await gems.update(str(made.id), {"name": " Gem Three ", "model": None})

    assert changed.name == "Gem Three"
    assert changed.model is None
    assert changed.instructions == "A patient tutor."
    assert changed.tools == ["web_search"]
    assert changed.created_at == made.created_at
    assert changed.updated_at > made.updated_at
    assert await gems.get(str(made.id)) == changed
    assert docdb.documents[made.id]["name"] == "Gem Three"


async def test_an_update_is_checked_like_a_new_gem() -> None:
    gems, _ = _gems()
    made = await gems.create(name="Gem Two")

    with pytest.raises(ValidationError):
        await gems.update(str(made.id), {"name": "  "})
    with pytest.raises(ValidationError):
        await gems.update(str(made.id), {"name": None})
    with pytest.raises(ValueError, match="cannot be changed"):
        await gems.update(str(made.id), {"created_at": datetime.now(UTC)})
    assert (await gems.get(str(made.id))).name == "Gem Two"


async def test_gems_are_listed_in_the_order_they_were_made_whatever_was_changed_since() -> None:
    gems, docdb = _gems()
    for name, made, changed in (("Second", 2, 9), ("Third", 3, 3), ("First", 1, 5)):
        document = _stored(
            name=name,
            created_at=datetime(2026, 11, made, tzinfo=UTC),
            updated_at=datetime(2026, 11, changed, tzinfo=UTC),
        )
        docdb.documents[document["_id"]] = document

    assert [gem.name for gem in await gems.list_all()] == ["First", "Second", "Third"]


async def test_a_deleted_gem_is_gone() -> None:
    gems, _ = _gems()
    made = await gems.create(name="Gem Two")

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
        await gems.update(gem_id, {"name": "Gem Three"})
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


def test_a_config_without_a_version_is_read_as_version_1_and_brought_up_to_date() -> None:
    # Version 1 is the first shape, so a config with no version cannot be older.
    # Reading it as version 1 lets a config written by hand leave the field out.
    document = _stored()
    del document["version"]

    assert upgrade_gem_document(document)["version"] == GEM_CONFIG_VERSION
    assert GemConfig.model_validate(document).version == GEM_CONFIG_VERSION


def test_a_version_1_config_gains_what_version_2_added_with_nothing_chosen() -> None:
    document = _stored(version=1, name="Gem Two", instructions="A patient tutor.")

    gem = GemConfig.model_validate(document)

    assert gem.version == GEM_CONFIG_VERSION
    assert (gem.description, gem.image, gem.voice, gem.speech_speed) == ("", None, None, 1.0)
    # What it already held is as it was, with what version 3 added after its instructions.
    assert (gem.name, gem.instructions, gem.tools) == (
        "Gem Two",
        f"A patient tutor.\n\n{DEFAULT_GEM_INSTRUCTIONS}",
        ["web_search"],
    )


async def test_a_version_1_config_is_stored_in_the_new_shape_once_it_is_changed() -> None:
    gems, docdb = _gems()
    document = _stored(version=1)
    docdb.documents[document["_id"]] = document

    await gems.update(str(document["_id"]), {"description": "Keeps the accounts."})

    stored = docdb.documents[document["_id"]]
    assert stored["version"] == GEM_CONFIG_VERSION
    assert stored["description"] == "Keeps the accounts."
    assert (stored["image"], stored["voice"], stored["speech_speed"]) == (None, None, 1.0)


@pytest.mark.parametrize(
    ("own", "instructions"),
    [
        ("A patient tutor.", f"A patient tutor.\n\n{DEFAULT_GEM_INSTRUCTIONS}"),
        ("A patient tutor.\n\n", f"A patient tutor.\n\n{DEFAULT_GEM_INSTRUCTIONS}"),
        ("", DEFAULT_GEM_INSTRUCTIONS),
        ("  \n", DEFAULT_GEM_INSTRUCTIONS),
    ],
)
def test_a_version_2_config_has_the_default_instructions_after_its_own(
    own: str, instructions: str
) -> None:
    # Before version 3 the code added the rules for a call and for writing to every gem.
    gem = GemConfig.model_validate(_stored(version=2, instructions=own))

    assert gem.version == GEM_CONFIG_VERSION
    assert gem.instructions == instructions


@pytest.mark.parametrize("version", [1, 2, 3])
def test_an_older_config_is_compacted_before_its_cache_expires(version: int) -> None:
    gem = GemConfig.model_validate(_stored(version=version))

    assert gem.version == GEM_CONFIG_VERSION
    assert gem.compaction == "before_expiry"


async def test_when_a_gem_is_compacted_is_made_changed_and_checked() -> None:
    gems, docdb = _gems()

    made = await gems.create(name="Gem Two", compaction="on_return")
    changed = await gems.update(str(made.id), {"compaction": "never"})

    assert made.compaction == "on_return"
    assert changed.compaction == docdb.documents[made.id]["compaction"] == "never"
    with pytest.raises(ValidationError):
        await gems.update(str(made.id), {"compaction": "sometimes"})


async def test_an_older_gem_with_long_instructions_still_loads_and_can_be_changed() -> None:
    # The limit is on what a user saves. The step to version 3 adds the defaults after
    # the gem's own text, which can take it past the limit: it must load all the same,
    # or the whole list of gems fails with it.
    gems, docdb = _gems()
    own = "x" * 19_900
    document = _stored(version=2, instructions=own)
    docdb.documents[document["_id"]] = document

    (listed,) = await gems.list_all()
    renamed = await gems.update(str(document["_id"]), {"name": "Gem Three"})

    assert len(listed.instructions) > 20_000
    assert renamed.instructions == listed.instructions == f"{own}\n\n{DEFAULT_GEM_INSTRUCTIONS}"
    with pytest.raises(ValidationError):
        await gems.update(str(document["_id"]), {"instructions": "x" * 20_001})
    with pytest.raises(ValidationError):
        await gems.create(name="Gem Two", instructions="x" * 20_001)


@pytest.mark.parametrize("version", [2, None])
async def test_an_older_gem_changed_twice_has_the_default_instructions_once(
    version: int | None,
) -> None:
    gems, docdb = _gems()
    document = _stored(version=version, instructions="A patient tutor.")
    if version is None:
        del document["version"]
    docdb.documents[document["_id"]] = document
    gem_id = str(document["_id"])
    upgraded = f"A patient tutor.\n\n{DEFAULT_GEM_INSTRUCTIONS}"

    # Only the name is changed: the upgraded instructions are stored with the new version.
    await gems.update(gem_id, {"name": "Gem Three"})
    assert docdb.documents[document["_id"]]["version"] == GEM_CONFIG_VERSION
    assert docdb.documents[document["_id"]]["instructions"] == upgraded
    assert (await gems.get(gem_id)).instructions == upgraded

    await gems.update(gem_id, {"name": "Gem Four"})
    read = await gems.get(gem_id)
    assert (read.name, read.instructions) == ("Gem Four", upgraded)
    assert docdb.documents[document["_id"]]["instructions"] == upgraded


@pytest.mark.parametrize("instructions", ["", "   ", "\n\n"])
async def test_a_gem_made_with_blank_instructions_is_given_the_defaults(instructions: str) -> None:
    gems, _ = _gems()

    gem = await gems.create(name="Gem Two", instructions=instructions)

    assert gem.instructions == DEFAULT_GEM_INSTRUCTIONS


async def test_a_gem_made_with_instructions_keeps_them_as_they_were_sent() -> None:
    gems, _ = _gems()

    gem = await gems.create(name="Gem Two", instructions="A patient tutor.")

    assert gem.instructions == "A patient tutor."


async def test_instructions_changed_to_blank_are_stored_blank() -> None:
    # The user removed them: they are not given the defaults again.
    gems, _ = _gems()
    made = await gems.create(name="Gem Two")

    changed = await gems.update(str(made.id), {"instructions": ""})

    assert changed.instructions == ""
    assert (await gems.get(str(made.id))).instructions == ""


async def test_a_gem_keeps_what_the_app_shows_of_it_and_how_it_is_to_be_spoken() -> None:
    gems, _ = _gems()

    made = await gems.create(
        name="Gem One",
        description="Looks after my money.",
        image="owl#6aa9ff",
        voice="Voice One",
        speech_speed=1.25,
    )
    read = await gems.get(str(made.id))

    assert (read.description, read.image, read.voice, read.speech_speed) == (
        "Looks after my money.",
        "owl#6aa9ff",
        "Voice One",
        1.25,
    )
    # The picture and the voice can be taken away again. The rest cannot be null.
    cleared = await gems.update(str(made.id), {"image": None, "voice": None})
    assert (cleared.image, cleared.voice) == (None, None)
    assert cleared.description == "Looks after my money."


@pytest.mark.parametrize(
    "fields",
    [
        {"description": "x" * 201},
        {"image": ""},
        {"image": "x" * 501},
        {"voice": ""},
        {"speech_speed": 0.4},
        {"speech_speed": 2.1},
    ],
)
async def test_a_gem_s_new_fields_keep_to_their_limits(fields: dict[str, Any]) -> None:
    gems, docdb = _gems()

    with pytest.raises(ValidationError):
        await gems.create(name="Gem One", **fields)
    assert docdb.documents == {}


def test_a_patch_may_set_the_picture_and_the_voice_to_null_but_not_the_rest() -> None:
    patch = GemsModels.V1.Input.UpdateGem.model_validate({"image": None, "voice": None})
    assert patch.changes() == {"image": None, "voice": None}

    for field in ("description", "speech_speed"):
        with pytest.raises(ValidationError, match="cannot be null"):
            GemsModels.V1.Input.UpdateGem.model_validate({field: None})


def _service() -> GemsServiceV1Class:
    return GemsServiceV1Class(_gems()[0])


def test_the_routes_are_the_ones_a_client_expects() -> None:
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
        # The gem's written conversation, for showing on screen.
        ("GET", "/v1/gems/{gem_id}/chat", None),
        # Resets it: the gem starts again, knowing nothing from before.
        ("POST", "/v1/gems/{gem_id}/reset", 204),
        ("GET", "/v1/gems/{gem_id}/log", None),
        ("GET", "/v1/gems/{gem_id}/log/{seq}", None),
        ("GET", "/v1/gems/{gem_id}/log/{seq}/sent", None),
        ("PATCH", "/v1/gems/{gem_id}", None),
        ("DELETE", "/v1/gems/{gem_id}", 204),
    }


async def test_the_service_hands_out_a_gem_with_its_id_as_text_and_its_version() -> None:
    service = _service()

    made = await service.create_gem(
        GemsModels.V1.Input.CreateGem(name="Gem Two", model="Haiku 5.5")
    )
    read = await service.get_gem(made.id)

    assert read == made
    assert ObjectId(made.id)
    assert made.version == GEM_CONFIG_VERSION
    assert made.tools == ["web_search"]
    # The app sends an empty text for a new gem; one sent with none is the same.
    assert made.instructions == DEFAULT_GEM_INSTRUCTIONS
    assert [gem.id for gem in await service.list_gems()] == [made.id]


async def test_the_service_changes_only_what_a_patch_sends() -> None:
    service = _service()
    made = await service.create_gem(
        GemsModels.V1.Input.CreateGem(name="Gem Two", model="Opus 5.5", speaks_first=True)
    )

    patch = GemsModels.V1.Input.UpdateGem.model_validate({"model": None})
    changed = await service.update_gem(made.id, patch)

    assert changed.model is None
    assert changed.name == "Gem Two"
    assert changed.speaks_first is True


async def test_the_service_deletes_and_then_cannot_find_a_gem() -> None:
    service = _service()
    made = await service.create_gem(GemsModels.V1.Input.CreateGem(name="Gem Two"))

    await service.delete_gem(made.id)

    with pytest.raises(NotFoundError):
        await service.get_gem(made.id)
    with pytest.raises(NotFoundError):
        await service.get_gem("not-an-id")


@pytest.mark.parametrize(
    "body",
    [
        {"name": "  "},
        {"name": "Gem Two", "model": "GPT-2"},
        {"name": "Gem Two", "tools": ["web_search", "web_search"]},
        {"name": "Gem Two", "tools": ["weather"]},
        {"name": "Gem Two", "instructions": "x" * 20_001},
    ],
)
def test_a_new_gem_that_breaks_a_rule_is_refused(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        GemsModels.V1.Input.CreateGem.model_validate(body)


@pytest.mark.parametrize("field", ["name", "instructions", "speaks_first", "tools"])
def test_a_patch_cannot_set_a_field_other_than_the_model_to_null(field: str) -> None:
    with pytest.raises(ValidationError, match="cannot be null"):
        GemsModels.V1.Input.UpdateGem.model_validate({field: None})


def test_a_patch_cannot_give_a_tool_there_is_not() -> None:
    with pytest.raises(ValidationError, match="cannot be given these tools"):
        GemsModels.V1.Input.UpdateGem.model_validate({"tools": ["weather"]})


def test_a_patch_keeps_only_the_fields_sent_and_a_null_model() -> None:
    patch = GemsModels.V1.Input.UpdateGem.model_validate({"name": " Gem Three ", "model": None})

    assert patch.changes() == {"name": "Gem Three", "model": None}
    assert GemsModels.V1.Input.UpdateGem().changes() == {}
