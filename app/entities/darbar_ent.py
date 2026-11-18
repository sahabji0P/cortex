"""
The Darbar text: what every gem is told first, ahead of its own name and instructions.

The user can change it. It is one document in the `Darbar` collection. When there is
none, the built-in text in `instructions.py` is used, so going back to it is deleting
the document. A call reads it once, when it opens, and keeps that text to its end.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from app.adapters.docdb_adp import DocDBAdapterClass
from app.entities.instructions import DEFAULT_DARBAR

__all__ = ["DARBAR_MAX_LENGTH", "Darbar", "DarbarEntityClass", "DarbarSetting", "DarbarText"]

# Long enough for a few paragraphs. It goes ahead of every gem's own words in every
# request, so a limit keeps a stray paste out of all of them.
DARBAR_MAX_LENGTH = 4_000

# The rule for the text, shared by the stored setting and the route's input. An empty
# text is allowed: it means gems are told nothing here.
type DarbarText = Annotated[str, Field(max_length=DARBAR_MAX_LENGTH)]

# There is only ever one Darbar text, so its document always has this id.
_ID = "darbar"


class DarbarSetting(BaseModel):
    """The Darbar text the user wrote, as it is stored."""

    __collection_name__: ClassVar[str] = "Darbar"

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default=_ID, alias="_id")
    # Stored as it was sent. Space around it is dropped only when a gem is told it.
    text: DarbarText
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Darbar:
    """The Darbar text in use, and whether it is the built-in one."""

    text: str
    is_default: bool


class DarbarEntityClass:
    """Read, set and reset the Darbar text."""

    def __init__(self, docdb: DocDBAdapterClass) -> None:
        self._docdb = docdb

    async def read(self) -> Darbar:
        """The text the user wrote, or the built-in one when they have written none.

        An empty text the user wrote is theirs, not the built-in one.
        """
        stored = await self._docdb.get(DarbarSetting, filters={"id": _ID})
        if stored is None:
            return Darbar(text=DEFAULT_DARBAR, is_default=True)
        return Darbar(text=stored.text, is_default=False)

    async def set(self, text: str) -> Darbar:
        # Checked here too, so a text that would not load is never stored.
        setting = DarbarSetting(text=text, updated_at=datetime.now(UTC))
        await self._docdb.update(
            DarbarSetting,
            {"id": _ID},
            setting.model_dump(exclude={"id"}),
            upsert=True,
        )
        return Darbar(text=setting.text, is_default=False)

    async def reset(self) -> Darbar:
        """Go back to the built-in text. Nothing to go back from is not an error."""
        await self._docdb.delete(DarbarSetting, {"id": _ID})
        return Darbar(text=DEFAULT_DARBAR, is_default=True)
