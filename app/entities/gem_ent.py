"""
A gem's config: one object that says who a gem is, kept in the `Gems` collection.

It is stored in the database but treated like a config file. It is the one home of a
gem: what a call or a chat acts on (its name, instructions, model, tools), and what the
app shows of it and wants spoken (its description, picture, voice and speed), which the
backend only keeps. Anything new is added by raising `GEM_CONFIG_VERSION` and writing one
step in `upgrade_gem_document` that brings older documents up to the new shape.

A call is held with a gem when it is opened with the gem's id, in `call.start` on the
standing socket. A chat names the gem in every `chat.send`.
"""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Annotated, Any, ClassVar, cast

from bson import ObjectId
from bson.errors import InvalidId
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from pydantic_mongo import PydanticObjectId

from app.adapters.docdb_adp import DocDBAdapterClass
from app.core.errors import NotFoundError
from app.core.times import UtcDatetime
from app.entities.chat_store import ChatStoreClass

# The one place the model names are imported. Everything else uses `GemModelName`.
from app.entities.model import MODELS, ModelName
from app.entities.tools import GEM_TOOL_NAMES

__all__ = [
    "DEFAULT_GEM_INSTRUCTIONS",
    "DEFAULT_GEM_TOOLS",
    "GEM_CONFIG_VERSION",
    "GemConfig",
    "GemDescription",
    "GemEntityClass",
    "GemImage",
    "GemInstructionsToSave",
    "GemModelName",
    "GemName",
    "GemSpeechSpeed",
    "GemTools",
    "GemToolsToSave",
    "GemVersionError",
    "GemVoice",
    "upgrade_gem_document",
]

# The version of the config's shape. Raise it when a field is added, renamed or changed.
GEM_CONFIG_VERSION = 3

# The models a gem can be answered by: the same names a call accepts.
type GemModelName = ModelName
_MODEL_NAMES = frozenset(info.name for info in MODELS)

# The tools a new gem may use, when it is not told which.
DEFAULT_GEM_TOOLS: tuple[str, ...] = ("web_search",)

# The instructions a new gem is given when it is made without any: how to talk in a call
# and how to write. They are the gem's own from then on, for the user to change or remove.
# A DRAFT THE OWNER HAS NOT READ YET.
DEFAULT_GEM_INSTRUCTIONS = """\
In a call:
- Talk like a person on a call: short, natural sentences. Keep to one or two sentences \
unless the user asks for more.
- Use plain words only: no markdown, lists, emoji, links or symbols. Write numbers the way \
they are said.
- If the user interrupts to ask or say something, answer that first, then go back to what \
you were saying if it is still worth finishing. If it was only a sound, or a word like \
"yes" or "okay", carry on from where you stopped. When you carry on after being cut off, \
continue from there instead of starting again.
- When what you are about to do will keep the user waiting for more than a second, such \
as looking something up, first say in a few words what you are going to do, the way a \
person would, and do it in that same turn: the user hears nothing else while they wait.

In writing:
- Write the way a person writes a message: plainly, and only as long as the answer needs.
- Write plain text, with no markdown or other formatting marks. The window shows your \
words exactly as you write them.
- When what you are about to do will keep the user waiting, such as looking something \
up, first say in a few words what you are going to do, the way a person would.

Always:
- When you answer from search results, name the source."""

_NAME_MAX_LENGTH = 100
# Long enough for a detailed brief. A limit keeps a stray paste out of every request.
_INSTRUCTIONS_MAX_LENGTH = 20_000
_TOOL_NAME_MAX_LENGTH = 64
_TOOLS_MAX = 32
_DESCRIPTION_MAX_LENGTH = 200
# Room for the address of a picture, should it become one.
_IMAGE_MAX_LENGTH = 500
_VOICE_MAX_LENGTH = 100


def _check_name(value: str) -> str:
    if not (name := value.strip()):
        raise ValueError("A gem's name cannot be blank")
    if len(name) > _NAME_MAX_LENGTH:
        raise ValueError(f"A gem's name is at most {_NAME_MAX_LENGTH} characters")
    return name


def _check_tools(value: list[str]) -> list[str]:
    names = [name.strip() for name in value]
    if any(not name for name in names):
        raise ValueError("A tool's name cannot be blank")
    # Refused rather than quietly dropped, so a client never sends one list and
    # gets back another.
    if len(set(names)) != len(names):
        raise ValueError("A tool is named more than once")
    return names


def _check_known_tools(value: list[str]) -> list[str]:
    if unknown := [name for name in value if name not in GEM_TOOL_NAMES]:
        raise ValueError(
            f"A gem cannot be given these tools: {', '.join(unknown)}. "
            f"The tools there are: {', '.join(GEM_TOOL_NAMES)}"
        )
    return value


# The rules for each field, shared by the stored config and the routes' input.
type GemName = Annotated[str, AfterValidator(_check_name)]
# What the app shows of a gem, and how its replies are to be spoken. The backend keeps
# these and gives them back. None of them changes what a call or a chat does yet.
type GemDescription = Annotated[str, Field(max_length=_DESCRIPTION_MAX_LENGTH)]
type GemImage = Annotated[str, Field(min_length=1, max_length=_IMAGE_MAX_LENGTH)]
type GemVoice = Annotated[str, Field(min_length=1, max_length=_VOICE_MAX_LENGTH)]
type GemSpeechSpeed = Annotated[float, Field(ge=0.5, le=2.0)]
type GemTools = Annotated[
    list[Annotated[str, Field(max_length=_TOOL_NAME_MAX_LENGTH)]],
    Field(max_length=_TOOLS_MAX),
    AfterValidator(_check_tools),
]
# Checked when a gem is made or changed, never when one is read. A stored config that
# names a tool which has since gone must still load: that tool is just not offered.
type GemToolsToSave = Annotated[GemTools, AfterValidator(_check_known_tools)]
_TOOLS_TO_SAVE: TypeAdapter[list[str]] = TypeAdapter(GemToolsToSave)
# The same for the instructions: the limit is on what a user saves. A stored config whose
# instructions a newer version made longer (see `_version_2_to_3`) must still load.
type GemInstructionsToSave = Annotated[str, Field(max_length=_INSTRUCTIONS_MAX_LENGTH)]
_INSTRUCTIONS_TO_SAVE: TypeAdapter[str] = TypeAdapter(GemInstructionsToSave)
# The same for the model: a name that is not offered is refused when a gem is made or
# changed. One already stored is read as no model at all (see `GemConfig._upgrade`).
_MODEL_TO_SAVE: TypeAdapter[GemModelName | None] = TypeAdapter(GemModelName | None)


class GemVersionError(ValueError):
    """A stored config is in a shape this code cannot read."""


def _version_1_to_2(document: dict[str, Any]) -> dict[str, Any]:
    """Version 2 added what the app shows of a gem and how it is to be spoken."""
    return {"description": "", "image": None, "voice": None, "speech_speed": 1.0, **document}


def _version_2_to_3(document: dict[str, Any]) -> dict[str, Any]:
    """Version 3 made the rules for a call and for writing part of a gem's instructions.

    Before, the code added them to every gem. Now a gem holds them in its own
    instructions, so an older gem's are its own text, a blank line, then the defaults.
    """
    own = str(document.get("instructions") or "").rstrip()
    instructions = (
        f"{own}\n\n{DEFAULT_GEM_INSTRUCTIONS}" if own.strip() else DEFAULT_GEM_INSTRUCTIONS
    )
    return {**document, "instructions": instructions}


# One step per version: the step under key N takes a document from version N to N + 1.
_UPGRADES: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {
    1: _version_1_to_2,
    2: _version_2_to_3,
}


def upgrade_gem_document(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return a stored config in the current shape, or refuse one from newer code.

    A document without a version is read as version 1. Version 1 is the first shape,
    so there is nothing older it could be, and a config written by hand need not
    name it. It is then brought up to date like any other version 1 document.
    """
    upgraded = dict(document)
    version = upgraded.get("version", 1)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise GemVersionError(f"A gem's config has no valid version: {version!r}")
    if version > GEM_CONFIG_VERSION:
        raise GemVersionError(
            f"A gem's config is version {version}, but this code knows only up to "
            f"version {GEM_CONFIG_VERSION}. It was written by a newer cortex."
        )
    while version < GEM_CONFIG_VERSION:
        upgraded = _UPGRADES[version](upgraded)
        version += 1
    upgraded["version"] = version
    return upgraded


class GemConfig(BaseModel):
    """Everything that makes a gem who it is, in the shape `GEM_CONFIG_VERSION` names."""

    __collection_name__: ClassVar[str] = "Gems"

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PydanticObjectId = Field(default_factory=ObjectId, alias="_id")
    version: int = GEM_CONFIG_VERSION
    name: GemName
    # One line on what the gem is for, shown under its name. The model is not told it.
    description: GemDescription = ""
    # The gem's picture, as one value the backend does not read. Today the app writes
    # its built-in face and colour there; later it could be the address of a picture.
    image: GemImage | None = None
    # Who this gem is and how it behaves, in plain words. A gem made without any is given
    # `DEFAULT_GEM_INSTRUCTIONS`.
    instructions: str = ""
    # The model that answers. None means the configured default.
    model: GemModelName | None = None
    # Whether the gem greets the user when a call connects.
    speaks_first: bool = False
    # The tools this gem may use, by name.
    tools: GemTools = Field(default_factory=lambda: list(DEFAULT_GEM_TOOLS))
    # The voice its replies are spoken in, and how fast: 1.0 is normal speed. Kept here
    # so they have one home. A call does not use them yet.
    voice: GemVoice | None = None
    speech_speed: GemSpeechSpeed = 1.0
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @model_validator(mode="before")
    @classmethod
    def _upgrade(cls, data: Any) -> Any:
        # Every read from the database comes through here, so every stored config is
        # brought to the current shape before its fields are checked.
        if not isinstance(data, Mapping):
            return data
        document = upgrade_gem_document(cast("Mapping[str, Any]", data))
        # A model that is no longer offered means the configured default, as a missing
        # one does. Otherwise one gem saved before a model went would fail to load, and
        # with it the whole list of gems.
        if document.get("model") not in _MODEL_NAMES:
            document["model"] = None
        return document


# The fields a client may change. Everything else is the store's to keep.
_EDITABLE = frozenset(
    {
        "name",
        "description",
        "image",
        "instructions",
        "model",
        "speaks_first",
        "tools",
        "voice",
        "speech_speed",
    }
)


def _gem_id(value: str) -> PydanticObjectId:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError) as err:
        raise NotFoundError("Gem") from err


class GemEntityClass:
    """Create, read, change and delete gems' configs."""

    def __init__(self, docdb: DocDBAdapterClass) -> None:
        self._docdb = docdb
        # A gem's written conversation is deleted with it.
        self._chat = ChatStoreClass(docdb)

    async def create(
        self,
        *,
        name: str,
        description: str = "",
        image: str | None = None,
        instructions: str = "",
        model: GemModelName | None = None,
        speaks_first: bool = False,
        tools: list[str] | None = None,
        voice: str | None = None,
        speech_speed: float = 1.0,
    ) -> GemConfig:
        """Make a gem. Instructions left out or blank are the defaults."""
        now = datetime.now(UTC)
        gem = GemConfig.model_validate(
            {
                # Named, so it is not read as an older config and upgraded.
                "version": GEM_CONFIG_VERSION,
                "name": name,
                "description": description,
                "image": image,
                "instructions": _INSTRUCTIONS_TO_SAVE.validate_python(instructions)
                if instructions.strip()
                else DEFAULT_GEM_INSTRUCTIONS,
                "model": _MODEL_TO_SAVE.validate_python(model),
                "speaks_first": speaks_first,
                "tools": _TOOLS_TO_SAVE.validate_python(
                    list(DEFAULT_GEM_TOOLS) if tools is None else tools
                ),
                "voice": voice,
                "speech_speed": speech_speed,
                "created_at": now,
                "updated_at": now,
            }
        )
        return await self._docdb.insert(gem)

    async def list_all(self) -> list[GemConfig]:
        """Every gem, in the order they were made.

        The order does not move when a gem is changed: the app shows them in a row, and
        a gem that jumped to the front on every edit would be hard to find.
        """
        return await self._docdb.get_many(GemConfig, filters={}, sort=[("created_at", 1)])

    async def get(self, gem_id: str) -> GemConfig:
        gem = await self._docdb.get(GemConfig, filters={"id": _gem_id(gem_id)})
        if gem is None:
            raise NotFoundError("Gem")
        return gem

    async def update(self, gem_id: str, changes: Mapping[str, Any]) -> GemConfig:
        """Change only the fields given. `updated_at` moves even when nothing else does.

        Instructions are stored as they are sent: blank ones mean the user removed them.
        """
        if unknown := set(changes) - _EDITABLE:
            raise ValueError(f"These fields of a gem cannot be changed: {sorted(unknown)}")
        if "tools" in changes:
            changes = {**changes, "tools": _TOOLS_TO_SAVE.validate_python(changes["tools"])}
        if "model" in changes:
            changes = {**changes, "model": _MODEL_TO_SAVE.validate_python(changes["model"])}
        if "instructions" in changes:
            changes = {
                **changes,
                "instructions": _INSTRUCTIONS_TO_SAVE.validate_python(changes["instructions"]),
            }
        current = await self.get(gem_id)
        # Checked as a whole, so a change can never leave a config that would not load.
        updated = GemConfig.model_validate(
            {
                **current.model_dump(by_alias=True),
                **changes,
                "updated_at": datetime.now(UTC),
            }
        )
        # The whole config is written, not just the changed fields. A config read in an
        # older shape is then stored in the current one, never half of each.
        document = updated.model_dump(by_alias=True, exclude={"id"})
        if await self._docdb.update(GemConfig, {"id": updated.id}, document) == 0:
            raise NotFoundError("Gem")
        return updated

    async def exists(self, gem_id: str) -> bool:
        """Whether there is a gem with this id, whether or not its config can be read."""
        try:
            found = _gem_id(gem_id)
        except NotFoundError:
            return False
        return await self._docdb.count(GemConfig, filters={"id": found}) > 0

    async def delete(self, gem_id: str) -> None:
        """Delete a gem, and its written conversation with it."""
        found = _gem_id(gem_id)
        if await self._docdb.delete(GemConfig, {"id": found}) == 0:
            raise NotFoundError("Gem")
        await self._chat.forget(found)
