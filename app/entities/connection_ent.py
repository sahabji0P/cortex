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

A server can let us in by a fixed header the user gives, or by a sign-in in the user's
browser (OAuth), which gives a pass. A sign-in is tried only for a connection with no
`Authorization` header of its own, when its server answers 401 and publishes how to
sign in. Then the connection is kept, with no tools, until the user signs in:

1. `begin_sign_in` finds the server's sign-in, registers this backend with it if need
   be, and gives the address the app opens in the browser. What it needs to finish is
   held in memory for 10 minutes, under a random `state` that is used once.
2. The browser comes back to this backend (`finish_sign_in`): the code it brings is
   swapped for a pass, the pass is kept, and the server is asked for its tools.

A pass that runs out is renewed without the user, when the server gave a refresh token:
just before it runs out, or when the server answers 401. One renewal at a time for a
connection. A pass that cannot be renewed is dropped the moment the server refuses it,
and the connection then needs a sign-in. Whoever listens (the app's sockets) is told at
once, and the gem is answered in words, so it can tell the user. A tool call never
waits on a browser.

The pass, its refresh token and the registration are kept in the database as they are
given, like the headers. None of it ever goes to a client, a frame, the model or a log.
"""

import asyncio
import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Collection, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, ClassVar, Literal, cast
from urllib.parse import urlsplit

from bson import ObjectId
from bson.errors import InvalidId
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from pydantic_mongo import PydanticObjectId

from app.adapters.docdb_adp import DocDBAdapterClass
from app.adapters.mcp_adp import (
    McpAdapterClass,
    McpAuthServer,
    McpChallenge,
    McpFailure,
    McpRegistration,
    McpServer,
    McpTokens,
    McpTool,
)
from app.config import settings
from app.core.errors import InvalidError, NotFoundError, UpstreamError
from app.core.times import UtcDatetime
from app.entities.gem_ent import GemEntityClass
from app.entities.model import ToolSpec
from app.entities.tools import Tool

__all__ = [
    "SIGN_IN_CALLBACK_PATH",
    "Connection",
    "ConnectionEntityClass",
    "ConnectionHeader",
    "ConnectionHeaders",
    "ConnectionName",
    "ConnectionTool",
    "ConnectionUrl",
    "GemScope",
    "Listener",
    "SignInOutcome",
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

# How many tools of its connections one gem is offered at most. A provider takes 128
# tools in one request, and the built-in ones and the ones only declared need room too.
_OFFERED_MAX = 100

# Where the user's browser comes back to this backend after a sign-in, under
# `settings.backend_url`. The route is defined in `app/routers/connections`.
SIGN_IN_CALLBACK_PATH = "/v1/connections/sign-in/callback"
# How long a sign-in that was begun may take to come back.
_SIGN_IN_SECONDS = 600.0
# How long before a pass runs out it is renewed, when it can be.
_RENEW_BEFORE = timedelta(minutes=1)
# How long a sign-in server's error code may be when it is shown.
_ERROR_SHOWN_MAX_LENGTH = 100

_log = logging.getLogger(__name__)

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
    # On, its tools are not told to the model up front: the model finds and loads them
    # with `tool_search` when it needs one. Off, they are sent in full with every request.
    deferred: bool = True
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
    # A sign-in, for a server that asks for one. None: the connection does not sign in.
    # Each is written on its own, and none is ever sent out of this backend.
    # How the server is signed in to, as its metadata says.
    oauth: McpAuthServer | None = None
    # This backend's registration with the sign-in server, from the last sign-in.
    oauth_client: McpRegistration | None = None
    # The pass. None: a sign-in is needed.
    oauth_tokens: McpTokens | None = None

    @property
    def server(self) -> McpServer:
        """The server, with the headers and, when it is signed in to, the pass."""
        headers = tuple((header.name, header.value) for header in self.headers)
        token = None
        if self.signs_in and self.oauth_tokens is not None:
            token = self.oauth_tokens.access_token
        return McpServer(self.url, headers, token)

    @property
    def own_authorization(self) -> bool:
        """Whether the user gave the `Authorization` header: then no sign-in is tried."""
        return any(header.name.lower() == "authorization" for header in self.headers)

    @property
    def signs_in(self) -> bool:
        """Whether this connection gets in by a sign-in."""
        return self.oauth is not None and not self.own_authorization

    @property
    def renews(self) -> bool:
        """Whether its pass can be renewed without the user."""
        tokens = self.oauth_tokens
        return tokens is not None and bool(tokens.refresh_token) and self.oauth_client is not None

    def signed_in(self, now: datetime | None = None) -> bool:
        """Whether it has a pass that may still let us in.

        One whose time is past and that cannot be renewed does not: it reads as needing
        a sign-in, worked out here when it is read. One with no known time may, until
        the server refuses it.
        """
        tokens = self.oauth_tokens
        if not self.signs_in or tokens is None:
            return False
        if self.renews or tokens.expires_at is None:
            return True
        return tokens.expires_at > (now or datetime.now(UTC))

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
_EDITABLE = frozenset({"name", "url", "headers", "enabled", "deferred", "gems", "tool_gems"})

# Told the id of a connection whose sign-in has changed: it needs one now, or one was
# just made. It may raise: that is not passed on.
type Listener = Callable[[str], Coroutine[Any, Any, None]]


@dataclass(frozen=True, slots=True)
class SignInOutcome:
    """How a sign-in that came back ended, in words for the user's browser."""

    signed_in: bool
    text: str


@dataclass(frozen=True, slots=True)
class _Pending:
    """A sign-in begun and not yet come back: what is needed to finish it.

    It is bound to the connection's address: if that changes meanwhile, the sign-in was
    for another server, and what it brings is not kept.
    """

    connection_id: PydanticObjectId
    url: str
    auth: McpAuthServer
    registration: McpRegistration = field(repr=False)
    verifier: str = field(repr=False)
    until: float  # on the monotonic clock


def _redirect_uri() -> str:
    """The address the user's browser is sent back to after a sign-in."""
    return f"{settings.backend_url.rstrip('/')}{SIGN_IN_CALLBACK_PATH}"


def _usable(registration: McpRegistration, auth: McpAuthServer, redirect_uri: str) -> bool:
    """Whether a registration kept from before can be used for a sign-in now.

    Not if it was made with another address to come back to, or with another sign-in
    server, or its secret has run out: it is made again then.
    """
    expires = registration.client_secret_expires_at
    return (
        registration.redirect_uri == redirect_uri
        and registration.issuer == auth.issuer
        and (not expires or expires > time.time())
    )


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


def _kept(key: str, listed: Sequence[McpTool]) -> tuple[list[ConnectionTool], int]:
    """The tools kept of a server's list, and how many were left out.

    Left out: a tool with no name, one whose name (as it is listed, or as the model
    would be told it) another tool already has, one whose schema cannot be sent (see
    `_sendable`), and whatever comes after the first `_TOOLS_MAX`. One tool a provider
    refuses fails the whole request, and with it every reply of every gem the
    connection is open to, so a doubtful one is left out rather than tried.
    """
    kept: dict[str, ConnectionTool] = {}
    offered: set[str] = set()
    for tool in listed:
        told = offered_name(key, tool.name)
        usable = (
            tool.name
            and tool.name not in kept
            and told not in offered
            and len(kept) < _TOOLS_MAX
            and _sendable(tool.input_schema)
        )
        if usable:
            offered.add(told)
            kept[tool.name] = ConnectionTool(
                name=tool.name,
                title=tool.title,
                description=tool.description.strip()[:_DESCRIPTION_MAX_LENGTH],
                input_schema=json.dumps(tool.input_schema, ensure_ascii=False),
            )
    return list(kept.values()), len(listed) - len(kept)


# What a provider takes as the name of one of a tool's arguments.
_ARGUMENT_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
# What neither provider takes at the top of a tool's schema.
_NOT_AT_THE_TOP = ("anyOf", "oneOf", "allOf", "enum", "not")


def _sendable(schema: Mapping[str, object]) -> bool:
    """Whether a tool's schema can be sent to a model with every request.

    It has to be an object's, a valid JSON schema, short enough, with nothing at its
    top that a provider refuses there, and with arguments named as a provider allows.
    NOT YET TRIED AGAINST EITHER PROVIDER: these are the rules as they are documented.
    """
    if schema.get("type") != "object" or any(word in schema for word in _NOT_AT_THE_TOP):
        return False
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return False
    names = cast("dict[object, object]", properties)
    if not all(isinstance(name, str) and _ARGUMENT_NAME.fullmatch(name) for name in names):
        return False
    try:
        Draft202012Validator.check_schema(schema)
        text = json.dumps(schema, ensure_ascii=False)
    except (SchemaError, TypeError, ValueError):
        return False
    return len(text) <= _SCHEMA_MAX_LENGTH


# The fields that hold what a server listed. Asking it again writes these and no others.
_LISTED = ("tools", "tools_left_out", "checked_at", "updated_at")
# The fields that hold a sign-in.
_SIGN_IN = ("oauth", "oauth_client", "oauth_tokens")


def _with_tools(connection: Connection, listed: Sequence[McpTool]) -> Connection:
    """The connection with the tools its server listed just now."""
    tools, left_out = _kept(connection.key, listed)
    now = datetime.now(UTC)
    return connection.model_copy(
        update={"tools": tools, "tools_left_out": left_out, "checked_at": now, "updated_at": now}
    )


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
        # Sign-ins begun and not yet come back, by their `state`. Kept in memory only:
        # one lost to a restart is begun again.
        self._pending: dict[str, _Pending] = {}
        # One for each connection: its pass is renewed, dropped and written under it,
        # and a new address is written under it, so a pass is never kept for another
        # server than the one it was given for.
        self._locks: dict[str, asyncio.Lock] = {}
        self._listeners: set[Listener] = set()
        # Telling listeners runs on its own, so a slow socket holds up no tool call.
        self._telling: set[asyncio.Task[None]] = set()

    def listen(self, listener: Listener) -> None:
        """Tell `listener` the id of each connection whose sign-in changes, until `ignore`."""
        self._listeners.add(listener)

    def ignore(self, listener: Listener) -> None:
        self._listeners.discard(listener)

    async def create(
        self,
        *,
        name: str,
        url: str,
        headers: Mapping[str, str] | None = None,
        gems: GemScope | None = None,
        enabled: bool = True,
        deferred: bool = True,
    ) -> Connection:
        """Add a connection: its server is asked for its tools, and they are kept.

        It is open to no gem unless `gems` says which. A server that asks for a sign-in
        this backend can do is kept with no tools, needing a sign-in. Raises
        `UpstreamError` if the server cannot be reached or does not answer, or refuses
        us with nothing to sign in by, and nothing is kept then.
        """
        now = datetime.now(UTC)
        connection = Connection(
            # Chosen below, once the server has answered: asking can take a while, and
            # a key chosen before it could be taken by then.
            key=_key_from(_check_name(name)),
            name=name,
            url=url,
            headers=_headers(headers or {}),
            enabled=enabled,
            deferred=deferred,
            gems=await self._scope([] if gems is None else gems),
            checked_at=now,
            created_at=now,
            updated_at=now,
        )
        asked = await self._ask(connection)
        connection = connection.model_copy(update={"key": await self._free_key(connection.key)})
        if isinstance(asked, McpAuthServer):
            return await self._docdb.insert(connection.model_copy(update={"oauth": asked}))
        return await self._docdb.insert(_with_tools(connection, asked))

    async def list_all(self) -> list[Connection]:
        """Every connection, in the order they were added."""
        return await self._docdb.get_many(Connection, filters={}, sort=[("created_at", 1)])

    async def get(self, connection_id: str) -> Connection:
        found = await self._docdb.get(Connection, filters={"id": _connection_id(connection_id)})
        if found is None:
            raise NotFoundError("Connection")
        return found

    async def update(self, connection_id: str, changes: Mapping[str, Any]) -> Connection:
        """Change only the fields given, and write only those.

        `headers` replaces every header. `tool_gems` maps a tool's name to the gems it
        is limited to, or to None to lift its limit; tools it does not name keep theirs.
        A new address or new headers have the server asked for its tools again, and
        nothing is changed if it does not answer.

        A new address is another server: the sign-in made for the old one is dropped,
        its pass and its registration, and the new server is signed in to if it asks.
        So is an `Authorization` header of the user's own: it takes a sign-in's place.
        """
        if unknown := set(changes) - _EDITABLE:
            raise ValueError(f"These fields of a connection cannot be changed: {sorted(unknown)}")
        before = await self.get(connection_id)
        fields: dict[str, Any] = {
            field: changes[field]
            for field in ("name", "url", "enabled", "deferred")
            if field in changes
        }
        if "headers" in changes:
            fields["headers"] = _headers(changes["headers"])
        moved = Connection.model_validate({**before.model_dump(by_alias=True), **fields})
        new_address = moved.url != before.url
        if new_address or (moved.own_authorization and before.oauth is not None):
            fields |= dict.fromkeys(_SIGN_IN)
            moved = moved.model_copy(update=dict.fromkeys(_SIGN_IN))
        asked = None
        if new_address or moved.headers != before.headers:
            asked = await self._ask(moved)
        async with self._lock(before.id):
            # Read again: asking the server can take a while, and who the connection is
            # open to may have been changed meanwhile. That change is not to be undone.
            current = await self.get(connection_id)
            if "gems" in changes:
                fields["gems"] = await self._scope(changes["gems"])
            # Checked as a whole, so a change can never leave a connection that would
            # not load.
            updated = Connection.model_validate(
                {**current.model_dump(by_alias=True), **fields, "updated_at": datetime.now(UTC)}
            )
            written = {*fields, "updated_at"}
            if isinstance(asked, McpAuthServer):
                updated = updated.model_copy(update={"oauth": asked})
                written.add("oauth")
                if new_address:
                    # The copy is of the old server's tools.
                    updated = updated.model_copy(update={"tools": [], "tools_left_out": 0})
                    written |= {"tools", "tools_left_out"}
            elif asked is not None:
                updated = _with_tools(updated, asked)
                written |= set(_LISTED)
            if "tool_gems" in changes:
                limits = await self._limits(updated, changes["tool_gems"])
                updated = updated.model_copy(update={"limits": limits})
                written.add("limits")
            await self._store(updated, written)
        if new_address:
            self._drop_pending(before.id)
        if asked is not None:
            await self._mcp.forget(before.server)
        return updated

    async def refresh(self, connection_id: str) -> Connection:
        """Ask the server for its tools again, and keep what it lists now.

        Raises `UpstreamError` if it does not answer. What was kept before stays then.
        Only the tools are written: who the connection is open to is not touched, even
        if it was changed while the server was being asked. A server that asks for a
        sign-in leaves the tools as they were, and the connection needs a sign-in.
        """
        before = await self.get(connection_id)
        asked = await self._ask(before)
        async with self._lock(before.id):
            current = await self.get(connection_id)
            if current.url != before.url:
                # Moved while it was asked: what came back is another server's.
                return current
            if isinstance(asked, McpAuthServer):
                updated = current.model_copy(update={"oauth": asked})
                await self._store(updated, {"oauth"})
            else:
                updated = _with_tools(current, asked)
                await self._store(updated, _LISTED)
        return updated

    async def delete(self, connection_id: str) -> None:
        current = await self.get(connection_id)
        if await self._docdb.delete(Connection, {"id": current.id}) == 0:
            raise NotFoundError("Connection")
        self._drop_pending(current.id)
        self._locks.pop(str(current.id), None)
        await self._mcp.forget(current.server)

    async def begin_sign_in(self, connection_id: str) -> str:
        """Begin a sign-in to the connection's server: the address to open in the browser.

        The server is asked with no pass, so its 401 says where its sign-in is. This
        backend is registered with the sign-in server if it is not yet, or if it was
        with another address to come back to. Raises `InvalidError` for a connection
        that does not sign in, and `UpstreamError` if the server's sign-in cannot be
        found or used.
        """
        connection = await self.get(connection_id)
        if connection.own_authorization:
            raise InvalidError(
                "This connection sends its own Authorization header, so it does not sign in"
            )
        bare = McpServer(connection.url, connection.server.headers)
        try:
            await self._mcp.list_tools(bare)
        except McpFailure as exc:
            if not exc.sign_in_needed:
                raise UpstreamError(
                    f"The server at {connection.url} could not be asked: {exc.reason}"
                ) from exc
            challenge = exc.challenge
        else:
            raise InvalidError(f"The server at {connection.url} lets Cortex in without a sign-in")
        try:
            auth = await self._mcp.discover(connection.url, challenge)
            if auth is None:
                raise McpFailure("it asks for a sign-in, but publishes no way to sign in")
            redirect_uri = _redirect_uri()
            registration = connection.oauth_client
            if registration is None or not _usable(registration, auth, redirect_uri):
                registration = await self._mcp.register(auth, redirect_uri)
        except McpFailure as exc:
            raise UpstreamError(
                f"The server at {connection.url} cannot be signed in to: {exc.reason}"
            ) from exc
        state = secrets.token_urlsafe(32)
        authorization = self._mcp.authorization(auth, registration, state)
        now = time.monotonic()
        # Sign-ins that never came back are let go.
        for late in [key for key, one in self._pending.items() if one.until <= now]:
            del self._pending[late]
        self._pending[state] = _Pending(
            connection_id=connection.id,
            url=connection.url,
            auth=auth,
            registration=registration,
            verifier=authorization.verifier,
            until=now + _SIGN_IN_SECONDS,
        )
        return authorization.url

    async def finish_sign_in(
        self, *, state: str | None, code: str | None, iss: str | None, error: str | None
    ) -> SignInOutcome:
        """A sign-in has come back from the browser: keep its pass, and ask for the tools.

        `state` is used once: it is taken before anything else is done, so the same
        answer brought twice is refused the second time. The pass, the registration and
        how the server is signed in to are written together: a pass is only ever kept
        with the client it was given to. The server is then asked for its tools, as a
        refresh does, and whoever listens is told.
        """
        pending = self._pending.pop(state, None) if state else None
        if pending is None or pending.until <= time.monotonic():
            return SignInOutcome(
                False,
                "This sign-in is not known here, or it took longer than 10 minutes. "
                "Start it again from Cortex.",
            )
        if error is not None:
            said = error[:_ERROR_SHOWN_MAX_LENGTH]
            return SignInOutcome(False, f"The sign-in was not finished. The server said: {said}")
        if not code:
            return SignInOutcome(False, "The sign-in came back without a code.")
        moved = SignInOutcome(
            False, "The connection was changed or removed during the sign-in. Start it again."
        )
        connection = await self._docdb.get(Connection, filters={"id": pending.connection_id})
        if connection is None or connection.url != pending.url or connection.own_authorization:
            return moved
        try:
            tokens = await self._mcp.exchange(
                pending.auth, pending.registration, code=code, verifier=pending.verifier, iss=iss
            )
        except McpFailure as exc:
            return SignInOutcome(False, f"Signing in to {connection.name} failed: {exc.reason}.")
        async with self._lock(pending.connection_id):
            current = await self._docdb.get(Connection, filters={"id": pending.connection_id})
            if current is None or current.url != pending.url or current.own_authorization:
                return moved
            signed = current.model_copy(
                update={
                    "oauth": pending.auth,
                    "oauth_client": pending.registration,
                    "oauth_tokens": tokens,
                }
            )
            await self._store(signed, _SIGN_IN)
        await self._mcp.forget(current.server)
        try:
            listed = await self._on_server(signed, self._mcp.list_tools)
        except McpFailure as exc:
            self._tell(signed.id)
            return SignInOutcome(
                True,
                f"Signed in to {signed.name}, but its tools could not be listed: {exc.reason}. "
                "Refresh the connection in Cortex.",
            )
        async with self._lock(signed.id):
            current = await self._docdb.get(Connection, filters={"id": signed.id})
            if current is not None and current.url == signed.url:
                await self._store(_with_tools(current, listed), _LISTED)
        self._tell(signed.id)
        return SignInOutcome(True, f"Signed in to {signed.name}. You can go back to Cortex.")

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
                changed = connection.model_copy(update={"gems": gems, "limits": limits})
                await self._store(changed, {"gems", "limits"})

    async def tools_for(self, gem_id: ObjectId) -> list[Tool]:
        """The connections' tools a gem is offered now, always in the same order.

        The connections in the order they were added, and each one's tools in the order
        its server listed them. A connection that needs a sign-in still offers the tools
        it has: a call of one is answered with why it cannot run, so the gem can tell
        the user, and the tool list does not move when a pass runs out.
        """
        gem = str(gem_id)
        connections = await self._docdb.get_many(
            Connection, filters={"enabled": True}, sort=[("created_at", 1)]
        )
        tools = [
            self._tool(connection, tool, gem)
            for connection in connections
            for tool in connection.tools
            if connection.open_to(gem, tool.name)
        ]
        if len(tools) > _OFFERED_MAX:
            # A provider refuses a request with more tools than it takes, and then the
            # gem could not answer at all. The last ones are passed over instead.
            _log.warning(
                "gem %s is open to %d tools of its connections: only the first %d are offered",
                gem,
                len(tools),
                _OFFERED_MAX,
            )
        return tools[:_OFFERED_MAX]

    def _tool(self, connection: Connection, tool: ConnectionTool, gem: str) -> Tool:
        """One tool of a connection, for the gem's toolbox."""
        spec = ToolSpec(
            name=offered_name(connection.key, tool.name),
            description=tool.description or tool.title or tool.name,
            parameters=cast("dict[str, object]", json.loads(tool.input_schema)),
            # The schema is the server's: it need not keep to what a strict one must.
            strict=False,
            deferred=connection.deferred,
        )
        label = f"{connection.name}: {tool.title or tool.name}"

        async def run(arguments: str) -> str:
            given = json.loads(arguments) if arguments.strip() else {}
            if not isinstance(given, dict):
                raise ValueError("the arguments are not an object")
            # Read again for every call, so that closing a tool to a gem holds from
            # that moment, and a changed address, header or pass is the one used.
            now = await self._docdb.get(Connection, filters={"id": connection.id})
            listed = now is not None and any(one.name == tool.name for one in now.tools)
            if now is None or not listed or not now.open_to(gem, tool.name):
                return "This tool is not available to you now."
            sent = cast("dict[str, object]", given)
            try:
                result = await self._on_server(
                    now, lambda server: self._mcp.call_tool(server, tool.name, sent)
                )
            except McpFailure as exc:
                if exc.sign_in_needed and now.signs_in:
                    # Said plainly, so the gem can tell the user why, and what to do.
                    again = " again" if now.oauth_client is not None else ""
                    return f"This tool needs the user to sign in to {now.name}{again}."
                raise
            text = _cut(result.text)
            if result.is_error:
                return f"The tool failed: {text}" if text else "The tool failed."
            return text

        return Tool(spec, run, shown=lambda arguments: label, connection=connection.name)

    async def _ask(self, connection: Connection) -> list[McpTool] | McpAuthServer:
        """The tools the connection's server lists now, or how to sign in to it.

        How to sign in, when the server answers 401 and publishes a sign-in this
        backend can do, and the connection has no `Authorization` header of its own.
        Raises `UpstreamError` otherwise.
        """
        try:
            return await self._on_server(connection, self._mcp.list_tools)
        except McpFailure as exc:
            failure = exc
        if failure.sign_in_needed and not connection.own_authorization:
            challenge = failure.challenge
            if challenge is None and connection.oauth is not None:
                # Not asked, as its pass is known to be gone: where its sign-in was
                # found before is looked at first.
                challenge = McpChallenge(connection.oauth.resource_metadata, None)
            try:
                auth = await self._mcp.discover(connection.url, challenge)
            except McpFailure as exc:
                raise UpstreamError(
                    f"The server at {connection.url} asks for a sign-in that Cortex cannot "
                    f"do: {exc.reason}"
                ) from exc
            if auth is not None:
                return auth
        raise UpstreamError(
            f"The server at {connection.url} could not be asked for its tools: {failure.reason}"
        ) from failure

    async def _on_server[T](
        self, connection: Connection, ask: Callable[[McpServer], Awaitable[T]]
    ) -> T:
        """Make one request of the connection's server, with its pass kept good.

        A pass about to run out is renewed first, and one the server refuses with 401
        is renewed and the request made once more. A pass that cannot be renewed is
        dropped, so the connection needs a sign-in, and an `McpFailure` that says
        `sign_in_needed` is raised, without asking the server when it is known. Nothing
        here waits on the user.
        """
        if connection.signs_in:
            connection = await self._fresh(connection)
            if not connection.signed_in():
                if connection.oauth_tokens is not None:
                    await self._mark(connection, connection.oauth_tokens)
                raise McpFailure("a sign-in is needed", sign_in_needed=True)
        try:
            return await ask(connection.server)
        except McpFailure as exc:
            used = connection.oauth_tokens
            if not exc.sign_in_needed or not connection.signs_in or used is None:
                raise
            failure = exc
        tokens = await self._renew(connection, used)
        if tokens is None:
            raise failure
        renewed = connection.model_copy(update={"oauth_tokens": tokens})
        try:
            return await ask(renewed.server)
        except McpFailure as exc:
            if exc.sign_in_needed:
                await self._mark(renewed, tokens)
            raise

    async def _fresh(self, connection: Connection) -> Connection:
        """The connection, with its pass renewed if it runs out within a minute and can be.

        A renewal that fails for now, while the pass has not yet run out, leaves it as it
        is: the pass still works. Raises `McpFailure` if it has run out and the sign-in
        server could not be reached.
        """
        tokens = connection.oauth_tokens
        if tokens is None or tokens.expires_at is None or not connection.renews:
            return connection
        now = datetime.now(UTC)
        if tokens.expires_at - now > _RENEW_BEFORE:
            return connection
        try:
            renewed = await self._renew(connection, tokens)
        except McpFailure:
            if tokens.expires_at > now:
                return connection
            raise
        return connection.model_copy(update={"oauth_tokens": renewed})

    async def _renew(self, connection: Connection, used: McpTokens) -> McpTokens | None:
        """A pass to use in place of `used`, renewed if need be. None: there is none.

        One renewal at a time for a connection. Once it is this one's turn, the stored
        pass is read again: if it is no longer `used`, another call renewed it, or the
        user signed in again, and that one is used. A pass the sign-in server will not
        renew is dropped. Raises `McpFailure` if the sign-in server could not be reached,
        and the pass is kept then.
        """
        async with self._lock(connection.id):
            current = await self._docdb.get(Connection, filters={"id": connection.id})
            if current is None or current.url != connection.url or not current.signs_in:
                return None
            stored = current.oauth_tokens
            if stored is None or stored.access_token != used.access_token:
                return stored
            if not current.renews or current.oauth is None or current.oauth_client is None:
                await self._drop_pass(current)
                return None
            try:
                tokens = await self._mcp.renew(
                    current.oauth, current.oauth_client, cast("str", stored.refresh_token)
                )
            except McpFailure as exc:
                if exc.sign_in_needed:
                    await self._drop_pass(current)
                    return None
                raise
            await self._store(current.model_copy(update={"oauth_tokens": tokens}), {"oauth_tokens"})
        await self._mcp.forget(current.server)
        return tokens

    async def _mark(self, connection: Connection, used: McpTokens) -> None:
        """The server refused `used`, and it cannot be renewed: the connection needs a sign-in.

        Nothing is changed if the stored pass is no longer that one.
        """
        async with self._lock(connection.id):
            current = await self._docdb.get(Connection, filters={"id": connection.id})
            stored = None if current is None else current.oauth_tokens
            if (
                current is not None
                and stored is not None
                and stored.access_token == used.access_token
            ):
                await self._drop_pass(current)

    async def _drop_pass(self, current: Connection) -> None:
        """Drop the pass, so the connection needs a sign-in, and say so. Its lock is held."""
        await self._store(current.model_copy(update={"oauth_tokens": None}), {"oauth_tokens"})
        await self._mcp.forget(current.server)
        self._tell(current.id)

    def _tell(self, connection_id: ObjectId) -> None:
        """Tell every listener that this connection's sign-in has changed, without waiting."""
        for listener in list(self._listeners):
            telling = asyncio.create_task(listener(str(connection_id)))
            self._telling.add(telling)
            telling.add_done_callback(_told)
            telling.add_done_callback(self._telling.discard)

    def _lock(self, connection_id: ObjectId) -> asyncio.Lock:
        return self._locks.setdefault(str(connection_id), asyncio.Lock())

    def _drop_pending(self, connection_id: ObjectId) -> None:
        """Let go of the sign-ins begun for a connection: what they bring is not kept."""
        for state in [
            key for key, one in self._pending.items() if one.connection_id == connection_id
        ]:
            del self._pending[state]

    async def _store(self, connection: Connection, fields: Collection[str]) -> None:
        """Write these fields of a connection, and no others.

        Each change writes only what it owns, so that one that waited on a server does
        not write back who the connection was open to before it waited, and a pass is
        written on its own.
        """
        document = connection.model_dump(by_alias=True, include=set(fields))
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


def _told(telling: asyncio.Task[None]) -> None:
    """A listener has been told, or could not be: a socket that has gone is not a fault."""
    if not telling.cancelled() and (failure := telling.exception()) is not None:
        _log.debug("a listener could not be told of a connection: %s", type(failure).__name__)


def _headers(headers: Mapping[str, str]) -> list[ConnectionHeader]:
    checked = _check_headers(dict(headers))
    return [ConnectionHeader(name=name, value=value) for name, value in checked.items()]
