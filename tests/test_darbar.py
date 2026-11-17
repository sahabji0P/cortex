"""The Darbar text: stored, read back, and the built-in one when nothing is stored."""

from collections.abc import MutableMapping
from typing import TYPE_CHECKING, Any, cast

import pytest
from pydantic import BaseModel, ValidationError

from app.entities.darbar_ent import Darbar, DarbarEntityClass, DarbarSetting

if TYPE_CHECKING:
    from app.adapters.docdb_adp import DocDBAdapterClass

# Written out in full, so a change to the built-in words shows up here.
_BUILT_IN = (
    "The user keeps a few assistants of their own, which they call gems. Each gem has its "
    "own name and its own work. The place they share is called the Darbar."
)


class _DocDBFake:
    """Holds the one Darbar document as a plain dict, as the real adapter would.

    It counts reads, so a test can say a call without a gem never made one.
    """

    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self.reads = 0

    @staticmethod
    def _id(filters: MutableMapping[str, Any]) -> str:
        assert set(filters) == {"id"}, filters
        return filters["id"]

    async def get[T: BaseModel](
        self, collection: type[T], *, filters: MutableMapping[str, Any]
    ) -> T | None:
        assert collection is DarbarSetting
        self.reads += 1
        document = self.documents.get(self._id(filters))
        return None if document is None else collection.model_validate(document)

    async def update(
        self,
        collection: type[BaseModel],
        filters: MutableMapping[str, Any],
        update_data: MutableMapping[str, Any],
        *,
        upsert: bool = False,
    ) -> int:
        assert collection is DarbarSetting
        # The id comes from the filter, as Mongo's upsert takes it, never from the data.
        assert "_id" not in update_data
        key = self._id(filters)
        if key not in self.documents:
            if not upsert:
                return 0
            self.documents[key] = {"_id": key}
        self.documents[key].update(update_data)
        return 1

    async def delete(self, collection: type[BaseModel], filters: MutableMapping[str, Any]) -> int:
        assert collection is DarbarSetting
        return 1 if self.documents.pop(self._id(filters), None) else 0


def _darbar() -> tuple[DarbarEntityClass, _DocDBFake]:
    docdb = _DocDBFake()
    return DarbarEntityClass(cast("DocDBAdapterClass", docdb)), docdb


# ---- the entity


async def test_with_nothing_stored_the_text_is_the_built_in_one() -> None:
    darbar, _ = _darbar()

    assert await darbar.read() == Darbar(text=_BUILT_IN, is_default=True)


async def test_a_text_that_is_set_is_stored_and_read_back_as_sent() -> None:
    darbar, docdb = _darbar()

    made = await darbar.set("  Every gem works for one person.\n")

    assert made == Darbar(text="  Every gem works for one person.\n", is_default=False)
    assert await darbar.read() == made
    assert list(docdb.documents) == ["darbar"]


async def test_setting_it_again_replaces_the_one_text() -> None:
    darbar, docdb = _darbar()

    await darbar.set("First.")
    await darbar.set("Second.")

    assert await darbar.read() == Darbar(text="Second.", is_default=False)
    assert len(docdb.documents) == 1


async def test_an_empty_text_is_the_user_s_not_the_built_in_one() -> None:
    darbar, _ = _darbar()

    await darbar.set("")

    assert await darbar.read() == Darbar(text="", is_default=False)


async def test_the_built_in_words_set_by_hand_are_still_the_user_s() -> None:
    # Only a reset goes back to the built-in text, so a later change to its words
    # reaches everyone who never wrote their own.
    darbar, _ = _darbar()

    await darbar.set(_BUILT_IN)

    assert await darbar.read() == Darbar(text=_BUILT_IN, is_default=False)


async def test_a_reset_goes_back_to_the_built_in_text() -> None:
    darbar, docdb = _darbar()
    await darbar.set("Something else.")

    reset = await darbar.reset()

    assert reset == Darbar(text=_BUILT_IN, is_default=True)
    assert await darbar.read() == reset
    assert docdb.documents == {}


async def test_a_reset_with_nothing_stored_is_not_an_error() -> None:
    darbar, _ = _darbar()

    assert await darbar.reset() == Darbar(text=_BUILT_IN, is_default=True)


async def test_the_text_is_at_most_4000_characters() -> None:
    darbar, docdb = _darbar()

    assert (await darbar.set("x" * 4_000)).text == "x" * 4_000
    with pytest.raises(ValidationError):
        await darbar.set("x" * 4_001)
    assert (await darbar.read()).text == "x" * 4_000
    assert len(docdb.documents) == 1
