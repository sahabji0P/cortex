"""
Connections: the tool servers gems can use, and which gem may use which.

A connection is one MCP server the user has added: where it is, the headers that let
this backend in, and a copy of the tools it listed when it was last asked. They are
kept in the `Connections` collection.

The copy is what a gem is offered, never the server's list of the moment. So the tools
a model is told of are the ones the user was shown: a server cannot change a tool's
description, or add a tool, behind the user's back. The copy changes only when the
connection is made, when its address or headers change, and when the user asks for it
to be checked again. It also keeps the tool list the same from one request to the next,
which a prompt cache needs, and costs no request to the server when a reply starts.

Who may use a connection is said on the connection, in two steps:

- `gems`: the gems it is open to, `"all"` or a list of ids. A new connection is open to
  none until the user says which.
- a limit on one tool: the gems that tool is open to. It can only narrow: a tool is
  offered to a gem when the connection is open to the gem and, if the tool has a limit,
  the limit names the gem too. A limit that names no gem turns the tool off.

That is checked twice. When a reply or a call starts, it decides which tools the gem is
told of. When a tool is called, the connection is read again: a call of a tool that has
since been closed to the gem is answered with why not, and a changed address or header
is the one used. So closing a connection to a gem holds from that moment, even in a
call that is already open.
"""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any, ClassVar, Literal, cast
from urllib.parse import urlsplit

from bson import ObjectId
from bson.errors import InvalidId
from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from pydantic_mongo import PydanticObjectId

from app.adapters.docdb_adp import DocDBAdapterClass
from app.adapters.mcp_adp import McpAdapterClass, McpFailure, McpServer, McpTool
from app.core.errors import InvalidError, NotFoundError, UpstreamError
from app.core.times import UtcDatetime
from app.entities.gem_ent import GemEntityClass
from app.entities.model import ToolSpec
from app.entities.tools import Tool

__all__ = [
    "Connection",
    "ConnectionEntityClass",
    "ConnectionHeader",
    "ConnectionHeaders",
    "ConnectionName",
    "ConnectionTool",
    "ConnectionUrl",
    "GemScope",
    "ToolLimit",
    "offered_name",
]

_NAME_MAX_LENGTH = 100
_URL_MAX_LENGTH = 2_000
_HEADERS_MAX = 20
_HEADER_VALUE_MAX_LENGTH = 4_000
# The key is the start of every one of the connection's tool names, as the model is told
# them. A tool's name is at most 64 characters, so the key is kept short.
_KEY_MAX_LENGTH = 20
_OFFERED_NAME_MAX_LENGTH = 64
# How many of a server's tools are kept, and how long a description and a schema may be.
# Every tool a gem is offered is sent with every request, so these keep one server from
# filling the context, or going past what a provider takes in one request.
_TOOLS_MAX = 128
_DESCRIPTION_MAX_LENGTH = 1_024
_SCHEMA_MAX_LENGTH = 20_000
# A result is kept whole in the conversation and sent with every request after it, so one
# is cut at this length. A FIRST NUMBER, TO BE TRIED OUT.
_RESULT_MAX_LENGTH = 20_000

# What HTTP allows in a header's name.
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
# Headers the protocol sets itself.
_HEADERS_NOT_OURS = frozenset({"accept", "content-length", "content-type", "host"})


def _check_name(value: str) -> str:
    if not (name := value.strip()):
        raise ValueError("A connection's name cannot be blank")
    if len(name) > _NAME_MAX_LENGTH:
        raise ValueError(f"A connection's name is at most {_NAME_MAX_LENGTH} characters")
    return name


def _check_url(value: str) -> str:
    url = value.strip()
    if len(url) > _URL_MAX_LENGTH:
        raise ValueError(f"A connection's address is at most {_URL_MAX_LENGTH} characters")
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError as err:
        raise ValueError("A connection's address is not a valid address") from err
    if parts.scheme not in ("http", "https") or not host:
        raise ValueError("A connection's address starts with http:// or https://")
    if parts.username is not None or parts.password is not None:
        # The address is shown again. What lets us in belongs in a header, which is not.
        raise ValueError("A connection's address cannot hold a user name or password")
    return url


def _check_headers(value: dict[str, str]) -> dict[str, str]:
    if len(value) > _HEADERS_MAX:
        raise ValueError(f"A connection has at most {_HEADERS_MAX} headers")
    seen: set[str] = set()
    for name, text in value.items():
        lower = name.lower()
        if not _HEADER_NAME.fullmatch(name):
            raise ValueError(f"{name!r} is not a header's name")
        if lower in _HEADERS_NOT_OURS or lower.startswith("mcp-"):
            raise ValueError(f"The {name} header is set by the protocol")
        if lower in seen:
            raise ValueError(f"The {name} header is given more than once")
        seen.add(lower)
        if not text or len(text) > _HEADER_VALUE_MAX_LENGTH or text != text.strip():
            raise ValueError(
                f"The {name} header's value is 1 to {_HEADER_VALUE_MAX_LENGTH} characters, "
                "with no space around it"
            )
        if any(ord(letter) < 0x20 or ord(letter) == 0x7F for letter in text):
            raise ValueError(f"The {name} header's value cannot hold a line break")
    return value


# The rules for each field, shared by the entity and the routes' input.
type ConnectionName = Annotated[str, AfterValidator(_check_name)]
type ConnectionUrl = Annotated[str, AfterValidator(_check_url)]
type ConnectionHeaders = Annotated[dict[str, str], AfterValidator(_check_headers)]
# The gems something is open to: every gem, or the ones named by id.
type GemScope = Literal["all"] | list[str]


class ConnectionHeader(BaseModel):
    """One header sent with every request to the server, such as the one that lets us in."""

    name: str
    value: str


class ConnectionTool(BaseModel):
    """One tool of a connection's server, as it was listed when the server was last asked."""

    name: str
    title: str | None = None
    description: str = ""
    # Its JSON schema, kept as text: a schema's own keys (`$ref`, say) are not ones a
    # document's fields may have.
    input_schema: str


class ToolLimit(BaseModel):
    """The gems one tool is open to, out of those the connection is open to."""

    tool: str
    gems: list[str]


class Connection(BaseModel):
    """One tool server the user has added, and who may use it."""

    __collection_name__: ClassVar[str] = "Connections"

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PydanticObjectId = Field(default_factory=ObjectId, alias="_id")
    # The start of its tools' names as the model is told them: `{key}__{tool}`. Made
    # from the name when the connection is made, and never changed after: the stored
    # conversations name the tools by it.
    key: str
    name: ConnectionName
    url: ConnectionUrl
    # Kept as they were given. No route sends their values back.
    headers: list[ConnectionHeader] = Field(default_factory=list[ConnectionHeader])
    # Off, no gem is offered its tools, and who it was open to is kept.
    enabled: bool = True
    gems: GemScope = Field(default_factory=list[str])
    tools: list[ConnectionTool] = Field(default_factory=list[ConnectionTool])
    # How many tools the server listed that were not kept (see `_kept`).
    tools_left_out: int = 0
    # Kept for a tool the server no longer lists, too: if the tool comes back, it comes
    # back as closed as the user left it.
    limits: list[ToolLimit] = Field(default_factory=list[ToolLimit])
    # When the server was last asked for its tools.
    checked_at: UtcDatetime
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @property
    def server(self) -> McpServer:
        return McpServer(self.url, tuple((header.name, header.value) for header in self.headers))

    def limit(self, tool: str) -> list[str] | None:
        """The gems a tool is limited to, or None if it has no limit."""
        return next((limit.gems for limit in self.limits if limit.tool == tool), None)

    def open_to(self, gem: str, tool: str) -> bool:
        """Whether the gem with this id may use this tool now."""
        if not self.enabled or (self.gems != "all" and gem not in self.gems):
            return False
        limit = self.limit(tool)
        return limit is None or gem in limit


# The fields a client may change. `tool_gems` changes the limits, one tool at a time.
_EDITABLE = frozenset({"name", "url", "headers", "enabled", "gems", "tool_gems"})


def offered_name(key: str, tool: str) -> str:
    """A connection's tool as the model is told its name: `{key}__{tool}`.

    A provider takes only letters, digits, `_` and `-`, and at most 64 characters. A
    name that does not fit is made to, and ends in a few characters worked out from the
    whole name, so two tools never end up with one name.
    """
    plain = re.sub(r"[^A-Za-z0-9_-]", "_", tool)
    name = f"{key}__{plain}"
    if plain == tool and len(name) <= _OFFERED_NAME_MAX_LENGTH:
        return name
    digest = hashlib.sha256(tool.encode()).hexdigest()[:6]
    room = _OFFERED_NAME_MAX_LENGTH - len(key) - len("__") - len("_") - len(digest)
    return f"{key}__{plain[:room]}_{digest}"


def _connection_id(value: str) -> PydanticObjectId:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError) as err:
        raise NotFoundError("Connection") from err


def _key_from(name: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:_KEY_MAX_LENGTH].strip("_")
    return key or "connection"


def _kept(listed: Sequence[McpTool]) -> tuple[list[ConnectionTool], int]:
    """The tools kept of a server's list, and how many were left out.

    Left out: a tool with no name or a name already listed, one whose schema is not an
    object's or is too long to send with every request, and whatever comes after the
    first `_TOOLS_MAX`.
    """
    kept: dict[str, ConnectionTool] = {}
    for tool in listed:
        schema = json.dumps(tool.input_schema, ensure_ascii=False)
        usable = (
            tool.name
            and tool.name not in kept
            and tool.input_schema.get("type") == "object"
            and len(schema) <= _SCHEMA_MAX_LENGTH
            and len(kept) < _TOOLS_MAX
        )
        if usable:
            kept[tool.name] = ConnectionTool(
                name=tool.name,
                title=tool.title,
                description=tool.description.strip()[:_DESCRIPTION_MAX_LENGTH],
                input_schema=schema,
            )
    return list(kept.values()), len(listed) - len(kept)


def _cut(text: str) -> str:
    if len(text) <= _RESULT_MAX_LENGTH:
        return text
    return (
        f"{text[:_RESULT_MAX_LENGTH]}\n[The result was {len(text)} characters long. "
        f"Only the first {_RESULT_MAX_LENGTH} are shown.]"
    )


class ConnectionEntityClass:
    """Add, read, change and remove connections, and say which tools a gem is offered."""

    def __init__(
        self, *, docdb: DocDBAdapterClass, mcp: McpAdapterClass, gems: GemEntityClass
    ) -> None:
        self._docdb = docdb
        self._mcp = mcp
        self._gems = gems

    async def create(
        self,
        *,
        name: str,
        url: str,
        headers: Mapping[str, str] | None = None,
        gems: GemScope | None = None,
        enabled: bool = True,
    ) -> Connection:
        """Add a connection: its server is asked for its tools, and they are kept.

        It is open to no gem unless `gems` says which. Raises `UpstreamError` if the
        server cannot be reached or does not answer, and nothing is kept then.
        """
        now = datetime.now(UTC)
        connection = Connection(
            key=await self._free_key(_key_from(_check_name(name))),
            name=name,
            url=url,
            headers=_headers(headers or {}),
            enabled=enabled,
            gems=await self._scope([] if gems is None else gems),
            checked_at=now,
            created_at=now,
            updated_at=now,
        )
        return await self._docdb.insert(await self._checked(connection))

    async def list_all(self) -> list[Connection]:
        """Every connection, in the order they were added."""
        return await self._docdb.get_many(Connection, filters={}, sort=[("created_at", 1)])

    async def get(self, connection_id: str) -> Connection:
        found = await self._docdb.get(Connection, filters={"id": _connection_id(connection_id)})
        if found is None:
            raise NotFoundError("Connection")
        return found

    async def update(self, connection_id: str, changes: Mapping[str, Any]) -> Connection:
        """Change only the fields given.

        `headers` replaces every header. `tool_gems` maps a tool's name to the gems it
        is limited to, or to None to lift its limit; tools it does not name keep theirs.
        A new address or new headers have the server asked for its tools again, and
        nothing is changed if it does not answer.
        """
        if unknown := set(changes) - _EDITABLE:
            raise ValueError(f"These fields of a connection cannot be changed: {sorted(unknown)}")
        current = await self.get(connection_id)
        fields: dict[str, Any] = {
            field: changes[field] for field in ("name", "url", "enabled") if field in changes
        }
        if "headers" in changes:
            fields["headers"] = _headers(changes["headers"])
        if "gems" in changes:
            fields["gems"] = await self._scope(changes["gems"])
        if "tool_gems" in changes:
            fields["limits"] = await self._limits(current, changes["tool_gems"])
        # Checked as a whole, so a change can never leave a connection that would not load.
        updated = Connection.model_validate(
            {**current.model_dump(by_alias=True), **fields, "updated_at": datetime.now(UTC)}
        )
        if updated.server != current.server:
            updated = await self._checked(updated)
        await self._store(updated)
        if updated.server != current.server:
            await self._mcp.forget(current.server)
        return updated

    async def refresh(self, connection_id: str) -> Connection:
        """Ask the server for its tools again, and keep what it lists now.

        Raises `UpstreamError` if it does not answer. What was kept before stays then.
        """
        updated = await self._checked(await self.get(connection_id))
        await self._store(updated)
        return updated

    async def delete(self, connection_id: str) -> None:
        current = await self.get(connection_id)
        if await self._docdb.delete(Connection, {"id": current.id}) == 0:
            raise NotFoundError("Connection")
        await self._mcp.forget(current.server)

    async def forget_gem(self, gem_id: str) -> None:
        """A gem has been deleted: it is taken out of every connection that names it."""
        for connection in await self.list_all():
            gems = connection.gems
            limits = [
                ToolLimit(tool=limit.tool, gems=[gem for gem in limit.gems if gem != gem_id])
                for limit in connection.limits
            ]
            if gems != "all":
                gems = [gem for gem in gems if gem != gem_id]
            if gems != connection.gems or limits != connection.limits:
                await self._store(connection.model_copy(update={"gems": gems, "limits": limits}))

    async def tools_for(self, gem_id: ObjectId) -> list[Tool]:
        """The connections' tools a gem is offered now, always in the same order.

        The connections in the order they were added, and each one's tools in the order
        its server listed them.
        """
        gem = str(gem_id)
        connections = await self._docdb.get_many(
            Connection, filters={"enabled": True}, sort=[("created_at", 1)]
        )
        return [
            self._tool(connection, tool, gem)
            for connection in connections
            for tool in connection.tools
            if connection.open_to(gem, tool.name)
        ]

    def _tool(self, connection: Connection, tool: ConnectionTool, gem: str) -> Tool:
        """One tool of a connection, for the gem's toolbox."""
        spec = ToolSpec(
            name=offered_name(connection.key, tool.name),
            description=tool.description or tool.title or tool.name,
            parameters=cast("dict[str, object]", json.loads(tool.input_schema)),
            # The schema is the server's: it need not keep to what a strict one must.
            strict=False,
        )
        label = f"{connection.name}: {tool.title or tool.name}"

        async def run(arguments: str) -> str:
            given = json.loads(arguments) if arguments.strip() else {}
            if not isinstance(given, dict):
                raise ValueError("the arguments are not an object")
            # Read again for every call, so that closing a tool to a gem holds from
            # that moment, and a changed address or header is the one used.
            now = await self._docdb.get(Connection, filters={"id": connection.id})
            listed = now is not None and any(one.name == tool.name for one in now.tools)
            if now is None or not listed or not now.open_to(gem, tool.name):
                return "This tool is not available to you now."
            result = await self._mcp.call_tool(
                now.server, tool.name, cast("dict[str, object]", given)
            )
            text = _cut(result.text)
            if result.is_error:
                return f"The tool failed: {text}" if text else "The tool failed."
            return text

        return Tool(spec, run, shown=lambda arguments: label)

    async def _checked(self, connection: Connection) -> Connection:
        """The connection with the tools its server lists now."""
        try:
            listed = await self._mcp.list_tools(connection.server)
        except McpFailure as exc:
            raise UpstreamError(
                f"The server at {connection.url} could not be asked for its tools: {exc.reason}"
            ) from exc
        tools, left_out = _kept(listed)
        now = datetime.now(UTC)
        return connection.model_copy(
            update={
                "tools": tools,
                "tools_left_out": left_out,
                "checked_at": now,
                "updated_at": now,
            }
        )

    async def _store(self, connection: Connection) -> None:
        # The whole document is written, as a gem's config is.
        document = connection.model_dump(by_alias=True, exclude={"id"})
        if await self._docdb.update(Connection, {"id": connection.id}, document) == 0:
            raise NotFoundError("Connection")

    async def _free_key(self, wanted: str) -> str:
        """`wanted`, or it with a number after it if another connection has that key."""
        taken = {connection.key for connection in await self.list_all()}
        key, number = wanted, 1
        while key in taken:
            number += 1
            ending = f"_{number}"
            key = f"{wanted[: _KEY_MAX_LENGTH - len(ending)]}{ending}"
        return key

    async def _scope(self, gems: GemScope) -> GemScope:
        """The scope with each gem's id as it is kept. Refuses a gem that is not there."""
        return "all" if gems == "all" else await self._gem_ids(gems)

    async def _gem_ids(self, gems: Sequence[str]) -> list[str]:
        # Refused rather than quietly dropped, so a client never sends one list and
        # gets back another.
        ids: list[str] = []
        for gem in gems:
            if not ObjectId.is_valid(gem) or not await self._gems.exists(gem):
                raise InvalidError(f"There is no gem with the id {gem}")
            if (found := str(ObjectId(gem))) not in ids:
                ids.append(found)
        return ids

    async def _limits(
        self, current: Connection, tool_gems: Mapping[str, Sequence[str] | None]
    ) -> list[ToolLimit]:
        """The connection's limits with these changes made."""
        known = {tool.name for tool in current.tools} | {limit.tool for limit in current.limits}
        if unknown := sorted(set(tool_gems) - known):
            raise InvalidError(f"The connection has no tool called: {', '.join(unknown)}")
        limits = {limit.tool: limit.gems for limit in current.limits}
        for tool, gems in tool_gems.items():
            if gems is None:
                limits.pop(tool, None)
            else:
                limits[tool] = await self._gem_ids(gems)
        return [ToolLimit(tool=tool, gems=gems) for tool, gems in limits.items()]


def _headers(headers: Mapping[str, str]) -> list[ConnectionHeader]:
    checked = _check_headers(dict(headers))
    return [ConnectionHeader(name=name, value=value) for name, value in checked.items()]
