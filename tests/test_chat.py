"""Written chat with a gem: what it is told, what is stored, and the standing socket.

The expected instructions are written out in full, here and in the Darbar's and the
call's tests, not built from the module's constants, so any change to what the model
reads shows up as a failing test.
"""

import asyncio
import json
from collections.abc import Iterable, MutableMapping, Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal, cast

import pytest
from bson import ObjectId
from pydantic import BaseModel

from app.core.errors import ConflictError, NotFoundError
from app.entities import chat_ent, conversation_ent, late_results
from app.entities.call_ent import CallEntityClass
from app.entities.chat_ent import ChatEntityClass
from app.entities.compaction import asked
from app.entities.conversation_ent import ConversationEntityClass, gem_key
from app.entities.conversation_store import (
    REMEMBERED,
    ConversationStoreClass,
    LogEntry,
    remembered,
)
from app.entities.darbar_ent import DarbarEntityClass
from app.entities.gem_ent import GEM_CONFIG_VERSION, GemConfig, GemEntityClass
from app.entities.history import Added
from app.entities.instructions import instructions
from app.routers.gems.service import GemsServiceV1Class
from app.routers.link.service import LinkServiceV1Class
from tests.test_model import _call_starts, _Claude, _final, _text
from tests.test_voice_conversation import (
    _assert_only_grew,
    _completed,
    _delta,
    _Search,
    _search_call,
    _starts,
)
from tests.test_voice_gem import _DARBAR
from tests.time_lines import TIME_LINE, is_time_line

if TYPE_CHECKING:
    from fastapi import WebSocket

    from app.adapters.docdb_adp import DocDBAdapterClass

_NOW = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)


def _a_gem(**fields: object) -> GemConfig:
    return GemConfig.model_validate(
        {
            "version": GEM_CONFIG_VERSION,
            "name": "Gem One",
            "instructions": "You look after the user's personal finances.",
            "created_at": _NOW,
            "updated_at": _NOW,
            **fields,
        }
    )


# ---- what a gem is told in writing


def _told(name: str, own: str, darbar: str = _DARBAR) -> str:
    return f"{darbar}You are {name}.\n\n{own}\n\n"


def test_a_gem_in_writing_is_told_what_it_is_told_in_a_call() -> None:
    # One instruction text for a gem: nothing in it depends on how the gem is reached.
    told = instructions(gem=_a_gem(), model_needs="- A need of the model's.\n")

    assert told == (
        _DARBAR
        + "You are Gem One.\n\nYou look after the user's personal finances.\n\n"
        + "- A need of the model's.\n\n"
    )


# ---- the store


def _name(collection: type[BaseModel]) -> str:
    return getattr(collection, "__collection_name__", collection.__name__)


class _DocDB:
    """The database adapter, faked: documents as plain dicts, one list per collection.

    Reads go through `model_validate`, as the real adapter's do. Times lose their zone
    on the way in, as they do in Mongo. A log's (gem_id, seq) is unique, as the real
    index makes it.
    """

    def __init__(self) -> None:
        self.collections: dict[str, list[dict[str, Any]]] = {}
        # When set, every write waits for it: a slow store.
        self.slow: Any = None
        # When set, a write of several documents stops after this many, and fails.
        self.breaks_after: int | None = None
        # When set, a write that holds a log entry of this kind fails, writing nothing.
        self.breaks_on_kind: str | None = None

    def _all(self, collection: type[BaseModel]) -> list[dict[str, Any]]:
        return self.collections.setdefault(_name(collection), [])

    @staticmethod
    def _matches(document: dict[str, Any], filters: MutableMapping[str, Any]) -> bool:
        def same(found: Any, wanted: Any) -> bool:
            if isinstance(wanted, dict) and set(wanted) == {"$in"}:
                return found in wanted["$in"]
            if isinstance(wanted, dict) and set(wanted) <= {"$gte", "$lt"}:
                # As in Mongo, a document without the field, or with none, is not found.
                return (
                    found is not None
                    and ("$gte" not in wanted or found >= wanted["$gte"])
                    and ("$lt" not in wanted or found < wanted["$lt"])
                )
            return found == wanted

        def at(key: str) -> Any:
            # As in Mongo, a dotted name reaches into a document's fields.
            found: Any = document
            for part in ("_id" if key == "id" else key).split("."):
                found = found.get(part) if isinstance(found, dict) else None
            return found

        return all(same(at(key), value) for key, value in filters.items())

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
        limit: int | None = None,
    ) -> list[T]:
        found = [d for d in self._all(collection) if self._matches(d, filters)]
        for field, direction in reversed(list(sort or [])):
            found.sort(key=lambda document: document[field], reverse=direction == -1)
        return [collection.model_validate(document) for document in found[:limit]]

    async def count(self, collection: type[BaseModel], *, filters: MutableMapping[str, Any]) -> int:
        return len([d for d in self._all(collection) if self._matches(d, filters)])

    async def insert[T: BaseModel](self, data: T) -> T:
        self._all(type(data)).append(self._stored(data))
        return data

    async def insert_many[T: BaseModel](self, data: Sequence[T]) -> list[T]:
        if self.slow is not None:
            await self.slow.wait()
        documents = self._all(type(data[0]))
        if self.breaks_on_kind is not None and any(
            isinstance(item, LogEntry) and item.kind == self.breaks_on_kind for item in data
        ):
            raise ConnectionError("the database went away")
        for written, item in enumerate(data):
            if self.breaks_after is not None and written == self.breaks_after:
                raise ConnectionError("the database went away part-way")
            document = self._stored(item)
            # No two entries of a gem's log share a number.
            if isinstance(item, LogEntry) and any(
                (d["gem_id"], d.get("seq")) == (document["gem_id"], document["seq"])
                for d in documents
            ):
                raise RuntimeError("duplicate key: that number is taken")
            documents.append(document)
        return list(data)

    async def update(
        self,
        collection: type[BaseModel],
        filters: MutableMapping[str, Any],
        update_data: MutableMapping[str, Any],
        *,
        upsert: bool = False,
    ) -> int:
        found = [d for d in self._all(collection) if self._matches(d, filters)]
        if not found and upsert:
            ids = {"_id" if key == "id" else key: value for key, value in filters.items()}
            self._all(collection).append({**ids, **update_data})
            return 1
        for document in found[:1]:
            # Times lose their zone on the way in here too, as they do in Mongo.
            document.update(
                {
                    key: value.replace(tzinfo=None) if isinstance(value, datetime) else value
                    for key, value in update_data.items()
                }
            )
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


def _stored_items(docdb: _DocDB) -> list[dict[str, Any]]:
    """What every gem remembers, as it is stored, without what else its log holds."""
    return [d for d in docdb.collections.get("GemLog", []) if d.get("kind") in REMEMBERED]


def _kept(gem_id: ObjectId, seq: int, item: Any, mode: str = "chat", **fields: Any) -> LogEntry:
    """The entry that remembers `item`, as entry `seq` of the gem's log."""
    return remembered(gem_id, seq, Added(item, _NOW, **fields), cast("Any", mode))


def _item(gem_id: ObjectId, seq: int, text: str, **fields: Any) -> LogEntry:
    return _kept(gem_id, seq, {"role": "user", "content": text}, **fields)


async def test_a_conversation_is_read_back_in_order_with_its_times_in_utc() -> None:
    docdb, adapter = _docdb()
    store = ConversationStoreClass(adapter)
    gem_id, other = ObjectId(), ObjectId()

    await store.write([_item(gem_id, 1, "second"), _item(gem_id, 2, "third")])
    await store.write([_item(other, 0, "another gem's")])
    await store.write([_item(gem_id, 0, "first")])

    read = await store.load(gem_id)
    assert [item.data.item for item in read] == [
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
        {"role": "user", "content": "third"},
    ]
    # Stored without a zone, as Mongo keeps them, and read back in UTC.
    assert _stored_items(docdb)[0]["at"].tzinfo is None
    assert all(item.at == _NOW and item.at.tzinfo is UTC for item in read)


async def test_every_shape_of_item_is_stored_as_it_was_given() -> None:
    _, adapter = _docdb()
    store = ConversationStoreClass(adapter)
    gem_id = ObjectId()
    items: list[Any] = [
        {"role": "user", "content": "weather?"},
        {"role": "assistant", "content": "Let me look."},
        {"type": "function_call", "call_id": "c1", "name": "web_search", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "Sunny."},
    ]

    await store.write([_kept(gem_id, seq, item) for seq, item in enumerate(items)])

    assert [item.data.item for item in await store.load(gem_id)] == items


async def test_deleting_a_gem_deletes_its_conversation() -> None:
    docdb, adapter = _docdb()
    gems, store = GemEntityClass(adapter), ConversationStoreClass(adapter)
    gem, other = await gems.create(name="Gem One"), await gems.create(name="Gem Two")
    await store.write([_item(gem.id, 0, "hello"), _item(gem.id, 1, "again")])
    await store.write([_item(other.id, 0, "hello")])

    await gems.delete(str(gem.id))

    assert await store.load(gem.id) == []
    assert len(await store.load(other.id)) == 1
    assert len(_stored_items(docdb)) == 1


# ---- chat over the standing socket, with the model and the search faked


class _LLM:
    """The OpenAI adapter, scripted: each request plays the next round of stream events.

    A round's `asyncio.Event` pauses the stream there until the test sets it. Every
    request starts with the whole of the one before it for the same conversation (told
    apart by its first message after the line that gives the time, which every one
    starts with), which is the rule the history keeps: the one thing that may change is
    the user's open turn, the last item, which can gain a line. A test that expects the
    next request to start cold says so (`starts_cold`): results may then be expired. A
    compaction's request, which ends with the message that asks for the summary, is no
    start for the next: that message is not part of the conversation.
    """

    configured = True

    def __init__(self, *rounds: list[Any]) -> None:
        self._rounds = list(rounds)
        self.requests: list[dict[str, Any]] = []
        self._cold = False

    def then(self, *rounds: list[Any]) -> None:
        self._rounds.extend(rounds)

    def starts_cold(self) -> None:
        """The next request may send results of the one before it expired, and only it."""
        self._cold = True

    async def stream_response(self, **request: Any) -> Any:
        sent = [dict(item) for item in request["input"]]
        same = [
            r
            for r in self.requests
            if _first(r["input"]) == _first(sent) and r["input"][-1:] != [asked()]
        ]
        if same:
            _assert_only_grew(same[-1]["input"], sent, cold=self._cold)
        self._cold = False
        self.requests.append({**request, "input": sent})
        script = self._rounds.pop(0)

        async def stream() -> Any:
            for step in script:
                if isinstance(step, asyncio.Event):
                    await step.wait()
                else:
                    yield step

        return stream()


def _first(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The first item of a request that is not the line that gives the time."""
    return [item for item in items if not is_time_line(item.get("content"))][:1]


class _NoKey:
    configured = False


def _said(text: str) -> list[Any]:
    return [_delta(text), _completed()]


def _tool_names(request: dict[str, Any]) -> list[str]:
    return [tool["name"] for tool in request["tools"]]


class _Socket:
    """A client of the standing socket. The test sends frames and reads what came back."""

    def __init__(self) -> None:
        self._inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.frames: list[dict[str, Any]] = []
        self.gone = False
        self._came = asyncio.Event()

    async def accept(self) -> None:
        pass

    async def receive(self) -> dict[str, Any]:
        return await self._inbox.get()

    async def send_text(self, text: str) -> None:
        if self.gone:
            raise RuntimeError("the socket is closed")
        self.frames.append(json.loads(text))
        self._came.set()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.gone = True

    def send(self, gem: object, text: str) -> None:
        self.raw(json.dumps({"type": "chat.send", "gem": str(gem), "text": text}))

    def raw(self, text: str | None = None, data: bytes | None = None) -> None:
        message: dict[str, Any] = {"type": "websocket.receive"}
        if data is not None:
            message["bytes"] = data
        else:
            message["text"] = text
        self._inbox.put_nowait(message)

    def hang_up(self) -> None:
        self.gone = True
        self._inbox.put_nowait({"type": "websocket.disconnect"})

    def chat(self) -> list[tuple[str, str]]:
        """The chat frames so far, as (type, text)."""
        return [(f["type"], f["text"]) for f in self.frames if f["type"].startswith("chat.")]

    async def until(self, type_: str, count: int = 1) -> None:
        """Wait until `count` frames of this type have come."""
        async with asyncio.timeout(2):
            while sum(f["type"] == type_ for f in self.frames) < count:
                self._came.clear()
                await self._came.wait()


class _Chat(SimpleNamespace):
    docdb: _DocDB
    gems: GemEntityClass
    store: ConversationStoreClass
    llm: _LLM
    search: Any
    conversations: ConversationEntityClass
    chat: ChatEntityClass
    gem: GemConfig

    def service(self) -> LinkServiceV1Class:
        # No call is held in these tests, so the call entity's voice is never reached.
        call = CallEntityClass(
            gems=self.gems,
            darbar=DarbarEntityClass(cast("Any", self.docdb)),
            voice=cast("Any", None),
            conversations=self.conversations,
        )
        return LinkServiceV1Class(self.chat, call)

    def again(self, **parts: Any) -> "_Chat":
        """The same database, read by new entities, as after a restart.

        `parts` stand in for the gems, the store, the two model adapters or the search.
        """
        gems = parts.get("gems", self.gems)
        conversations = ConversationEntityClass(gems=gems, store=parts.get("store", self.store))
        chat = ChatEntityClass(
            gems=gems,
            darbar=DarbarEntityClass(cast("Any", self.docdb)),
            conversations=conversations,
            openai=parts.get("openai", cast("Any", self.llm)),
            anthropic=parts.get("anthropic", cast("Any", _NoKey())),
            search=parts.get("search", self.search),
        )
        return _Chat(**{**vars(self), "conversations": conversations, "chat": chat})

    async def stored(self) -> list[Any]:
        return [item.data.item for item in await self.store.load(self.gem.id)]

    async def shown(self) -> list[tuple[str, str]]:
        chat = await GemsServiceV1Class(self.gems, self.conversations).get_chat(str(self.gem.id))
        return [(message.role, message.text) for message in chat.messages]


async def _chat(*rounds: list[Any], search: Any = None, **gem: Any) -> _Chat:
    docdb, adapter = _docdb()
    gems, store = GemEntityClass(adapter), ConversationStoreClass(adapter)
    llm = _LLM(*rounds)
    search = _Search() if search is None else search
    conversations = ConversationEntityClass(gems=gems, store=store)
    chat = ChatEntityClass(
        gems=gems,
        darbar=DarbarEntityClass(adapter),
        conversations=conversations,
        openai=cast("Any", llm),
        anthropic=cast("Any", _NoKey()),
        search=search,
    )
    made = await gems.create(
        **{
            "name": "Gem One",
            "instructions": "You look after the user's personal finances.",
            "model": "6 Luna",
            **gem,
        }
    )
    return _Chat(
        docdb=docdb,
        gems=gems,
        store=store,
        llm=llm,
        search=search,
        conversations=conversations,
        chat=chat,
        gem=made,
    )


async def _open(service: LinkServiceV1Class) -> tuple[_Socket, asyncio.Task[None]]:
    socket = _Socket()
    linked = asyncio.create_task(service.link(cast("WebSocket", socket)))
    await socket.until("link.ready")
    return socket, linked


async def _close(socket: _Socket, linked: asyncio.Task[None]) -> None:
    socket.hang_up()
    async with asyncio.timeout(2):
        await linked


async def test_a_reply_is_streamed_stored_and_shown() -> None:
    setup = await _chat([_delta("Hi "), _delta("there."), _completed()])
    socket, linked = await _open(setup.service())

    socket.send(setup.gem.id, "hello")
    await socket.until("chat.done")

    assert socket.frames[0] == {"type": "link.ready", "version": 8}
    assert socket.frames[1:] == [
        {"type": "chat.text", "gem": str(setup.gem.id), "text": "Hi "},
        {"type": "chat.text", "gem": str(setup.gem.id), "text": "there."},
        {"type": "chat.done", "gem": str(setup.gem.id), "text": "Hi there."},
    ]
    assert await setup.stored() == [
        TIME_LINE,
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi there."},
    ]
    assert await setup.shown() == [("user", "hello"), ("gem", "Hi there.")]
    [request] = setup.llm.requests
    assert request["instructions"] == _told(
        "Gem One", "You look after the user's personal finances."
    )
    # Search, and as in a call, a way to stop one that carries on in the background.
    assert _tool_names(request) == ["web_search", "cancel_tool_call"]
    await _close(socket, linked)


async def _nowhere(event: Any) -> None:
    pass  # the frames of a reply, which these tests do not read


async def test_a_message_gets_the_time_again_once_the_last_line_is_over_ten_minutes_old() -> None:
    setup = await _chat(
        [_delta("One."), _completed()],
        [_delta("Two."), _completed()],
        [_delta("Three."), _completed()],
    )
    gem = str(setup.gem.id)

    await setup.chat.send(gem, "one", _nowhere, time_zone="Asia/Kolkata").over.wait()
    # A minute later: the line before the first message still holds.
    await setup.chat.send(gem, "two", _nowhere, time_zone="Asia/Kolkata").over.wait()
    # The line is made eleven minutes old, as if the user came back later.
    for entry in setup.docdb.collections[_name(LogEntry)]:
        if entry["data"].get("source") == "time-line":
            entry["at"] -= timedelta(minutes=11)
    await setup.chat.send(gem, "three", _nowhere, time_zone="Asia/Kolkata").over.wait()

    stored = await setup.stored()
    assert stored == [
        TIME_LINE,
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "One."},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "Two."},
        TIME_LINE,
        {"role": "user", "content": "three"},
        {"role": "assistant", "content": "Three."},
    ]
    # In the zone the app sent.
    assert stored[0]["content"].endswith(" in Asia/Kolkata.")
    # The model is sent the line as it is stored.
    assert setup.llm.requests[-1]["input"] == stored[:-1]


@pytest.mark.parametrize(
    ("time_zone", "ends"),
    [
        ("Asia/Kolkata", " in Asia/Kolkata."),
        # Anything that is not a zone is no reason to refuse the message.
        (5, " UTC. The user's time zone is not known."),
        (None, " UTC. The user's time zone is not known."),
    ],
)
async def test_chat_send_carries_the_users_time_zone(time_zone: object, ends: str) -> None:
    setup = await _chat([_delta("Hi."), _completed()])
    socket, linked = await _open(setup.service())

    socket.raw(
        json.dumps(
            {"type": "chat.send", "gem": str(setup.gem.id), "text": "hi", "time_zone": time_zone}
        )
    )
    await socket.until("chat.done")

    first = (await setup.stored())[0]
    assert first == TIME_LINE
    assert first["content"].endswith(ends)
    await _close(socket, linked)


async def test_a_zone_that_cannot_be_read_gives_the_time_in_utc() -> None:
    setup = await _chat([_delta("Hi."), _completed()])

    await setup.chat.send(str(setup.gem.id), "hi", _nowhere, time_zone="Mars/Olympus").over.wait()

    first = (await setup.stored())[0]
    assert first == TIME_LINE
    assert first["content"].endswith(" UTC. The user's time zone is not known.")


async def test_the_next_message_is_sent_the_stored_conversation() -> None:
    setup = await _chat(_said("Hi."), _said("Fine."))
    socket, linked = await _open(setup.service())
    socket.send(setup.gem.id, "hello")
    await socket.until("chat.done")
    await _close(socket, linked)

    # Another socket, and another chat entity: only the database is shared.
    later = setup.again()
    socket, linked = await _open(later.service())
    socket.send(later.gem.id, "how are you?")
    await socket.until("chat.done")

    first, second = setup.llm.requests
    assert second["input"] == [
        *first["input"],
        {"role": "assistant", "content": "Hi."},
        {"role": "user", "content": "how are you?"},
    ]
    stored = await setup.store.load(setup.gem.id)
    # Each is the kind of entry it is, and they are read back in the order of their numbers.
    # The second message comes seconds after the first: the time is given before the first only.
    assert [item.kind for item in stored] == [
        "note.written",
        "user.said",
        "gem.said",
        "user.said",
        "gem.said",
    ]
    assert [item.seq for item in stored] == sorted(item.seq for item in stored)
    await _close(socket, linked)


async def test_a_new_message_cuts_the_reply_being_written() -> None:
    never = asyncio.Event()
    setup = await _chat([_delta("Once upon "), _delta("a time "), never], _said("Sure."))
    socket, linked = await _open(setup.service())

    socket.send(setup.gem.id, "a story")
    await socket.until("chat.text", 2)
    socket.send(setup.gem.id, "actually, the weather")
    await socket.until("chat.done")

    assert socket.chat() == [
        ("chat.text", "Once upon "),
        ("chat.text", "a time "),
        ("chat.cut", "Once upon a time…"),
        ("chat.text", "Sure."),
        ("chat.done", "Sure."),
    ]
    assert await setup.stored() == [
        TIME_LINE,
        {"role": "user", "content": "a story"},
        {"role": "assistant", "content": "Once upon a time…"},
        {"role": "user", "content": "actually, the weather"},
        {"role": "assistant", "content": "Sure."},
    ]
    assert setup.llm.requests[1]["input"] == (await setup.stored())[:4]
    await _close(socket, linked)


async def test_the_cut_goes_to_the_socket_whose_reply_it_was() -> None:
    never = asyncio.Event()
    setup = await _chat([_delta("Once upon "), never], _said("Sure."))
    service = setup.service()
    one, one_linked = await _open(service)
    two, two_linked = await _open(service)

    one.send(setup.gem.id, "a story")
    await one.until("chat.text")
    two.send(setup.gem.id, "never mind")
    await two.until("chat.done")

    assert one.chat() == [("chat.text", "Once upon "), ("chat.cut", "Once upon…")]
    assert two.chat() == [("chat.text", "Sure."), ("chat.done", "Sure.")]
    await _close(one, one_linked)
    await _close(two, two_linked)


async def test_messages_faster_than_the_reply_are_each_cut_in_turn() -> None:
    never = asyncio.Event()
    setup = await _chat([_delta("Once upon "), never], _said("Sure."))
    socket, linked = await _open(setup.service())

    socket.send(setup.gem.id, "a story")
    await socket.until("chat.text")
    socket.send(setup.gem.id, "no wait")
    socket.send(setup.gem.id, "the weather")
    await socket.until("chat.done")

    assert socket.chat() == [
        ("chat.text", "Once upon "),
        ("chat.cut", "Once upon…"),
        # The middle message is cut before its reply begins: the model is not asked.
        ("chat.cut", "…"),
        ("chat.text", "Sure."),
        ("chat.done", "Sure."),
    ]
    assert len(setup.llm.requests) == 2
    # No reply got through to the middle message. It is stored as it was sent, and the
    # last one follows it: the reply answers both.
    stored = await setup.stored()
    assert stored[0] == TIME_LINE
    assert [item["content"] for item in stored[1:]] == [
        "a story",
        "Once upon…",
        "no wait",
        "the weather",
        "Sure.",
    ]
    await _close(socket, linked)


async def test_a_message_sent_before_any_of_the_reply_got_through_follows_the_first() -> None:
    pause = asyncio.Event()
    setup = await _chat([pause, _delta("Never said.")], _said("Sunny."))
    socket, linked = await _open(setup.service())

    socket.send(setup.gem.id, "a story")
    await _eventually_asked(setup, 1)
    socket.send(setup.gem.id, "no, the weather")
    await socket.until("chat.done")

    assert socket.chat() == [("chat.cut", "…"), ("chat.text", "Sunny."), ("chat.done", "Sunny.")]
    # The model was asked about the first message, and is then asked about both: the
    # second follows the first, and nothing it was sent before has changed.
    first, second = setup.llm.requests
    assert first["input"] == [TIME_LINE, {"role": "user", "content": "a story"}]
    assert second["input"] == [
        TIME_LINE,
        {"role": "user", "content": "a story"},
        {"role": "user", "content": "no, the weather"},
    ]
    assert await setup.stored() == [
        TIME_LINE,
        {"role": "user", "content": "a story"},
        {"role": "user", "content": "no, the weather"},
        {"role": "assistant", "content": "Sunny."},
    ]
    assert await setup.shown() == [
        ("user", "a story"),
        ("user", "no, the weather"),
        ("gem", "Sunny."),
    ]
    await _close(socket, linked)


async def _eventually_asked(setup: _Chat, count: int) -> None:
    """Wait until the model has been sent `count` requests."""
    async with asyncio.timeout(2):
        while True:
            if len(setup.llm.requests) >= count:
                return
            await asyncio.sleep(0)


async def test_a_tool_call_is_shown_its_result_sent_back_and_both_stored() -> None:
    call = _search_call("Pune weather")
    setup = await _chat(
        [_delta("Let me look."), _starts(call), _completed(call)],
        _said("Sunny."),
        _said("Bye."),
    )
    socket, linked = await _open(setup.service())

    socket.send(setup.gem.id, "weather?")
    await socket.until("chat.done")
    socket.send(setup.gem.id, "thanks")
    await socket.until("chat.done", 2)

    assert socket.chat()[:4] == [
        ("chat.text", "Let me look."),
        ("chat.tool", "Pune weather"),
        ("chat.text", "Sunny."),
        ("chat.done", "Let me look. Sunny."),
    ]
    assert setup.search.queries == ["Pune weather"]
    result = setup.llm.requests[1]["input"][4]
    assert result["type"] == "function_call_output"
    assert "Norris won in Las Vegas." in result["output"]
    stored = await setup.stored()
    assert stored[0] == TIME_LINE
    assert stored[:6] == [
        *setup.llm.requests[1]["input"][:5],
        {"role": "assistant", "content": "Sunny."},
    ]
    assert setup.llm.requests[2]["input"] == [*stored[:6], {"role": "user", "content": "thanks"}]
    assert await setup.shown() == [
        ("user", "weather?"),
        ("gem", "Let me look."),
        ("tool", "Pune weather"),
        ("gem", "Sunny."),
        ("user", "thanks"),
        ("gem", "Bye."),
    ]
    await _close(socket, linked)


async def test_a_tool_still_running_at_a_cut_is_stored_with_the_placeholder() -> None:
    call = _search_call("Pune weather")
    setup = await _chat(
        [_delta("Let me look."), _starts(call), _completed(call)],
        _said("Okay."),
        search=_Search(gate=asyncio.Event()),
    )
    socket, linked = await _open(setup.service())

    socket.send(setup.gem.id, "weather?")
    await socket.until("chat.tool")
    socket.send(setup.gem.id, "never mind")
    await socket.until("chat.done")

    assert socket.chat() == [
        ("chat.text", "Let me look."),
        ("chat.tool", "Pune weather"),
        ("chat.cut", "Let me look."),
        ("chat.text", "Okay."),
        ("chat.done", "Okay."),
    ]
    stored = await setup.stored()
    assert stored[0] == TIME_LINE
    assert [item.get("role") or item["type"] for item in stored] == [
        "developer",
        "user",
        "assistant",
        "function_call",
        "function_call_output",
        "user",
        "assistant",
    ]
    assert stored[4]["output"].startswith("Still running in the background, under call_id call_1.")
    assert setup.llm.requests[1]["input"] == stored[:6]
    await _close(socket, linked)


@pytest.mark.parametrize(
    ("which", "reason"),
    [
        ("missing", "unknown gem"),
        ("not an id", "unknown gem"),
        ("unreadable", "the gem's config cannot be read"),
        ("no key", "no Anthropic key is set, so Haiku 5.5 cannot be used"),
    ],
)
async def test_a_message_no_reply_can_be_made_to_gets_chat_error_and_stores_nothing(
    which: str, reason: str
) -> None:
    setup = await _chat(model="Haiku 5.5" if which == "no key" else "6 Luna")
    gem_id = {"missing": str(ObjectId()), "not an id": "not-an-id"}.get(which, str(setup.gem.id))
    if which == "unreadable":
        [document] = setup.docdb.collections["Gems"]
        document["version"] = 99
    socket, linked = await _open(setup.service())

    socket.send(gem_id, "hello")
    await socket.until("chat.error")

    assert socket.frames[1:] == [{"type": "chat.error", "gem": gem_id, "text": reason}]
    assert _stored_items(setup.docdb) == []
    assert setup.llm.requests == []
    await _close(socket, linked)


async def test_a_reply_that_fails_part_way_keeps_what_was_sent() -> None:
    setup = await _chat([_delta("Half "), SimpleNamespace(type="error", message="boom")])
    socket, linked = await _open(setup.service())

    socket.send(setup.gem.id, "go")
    await socket.until("chat.error")

    assert socket.chat() == [("chat.text", "Half "), ("chat.error", "reply failed: boom")]
    assert await setup.stored() == [
        TIME_LINE,
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "Half…"},
    ]
    await _close(socket, linked)


async def test_a_reply_that_cannot_be_stored_ends_in_chat_error() -> None:
    setup = await _chat(_said("Hi."), _said("Hi again."))
    socket, linked = await _open(setup.service())
    # The database takes nothing.
    setup.docdb.breaks_after = 0

    socket.send(setup.gem.id, "hello")
    await socket.until("chat.error")

    assert socket.chat()[-1] == (
        "chat.error",
        "the reply could not be stored: the database went away part-way",
    )
    assert await setup.stored() == []
    # What could not be stored is dropped, not kept for later: the next reply starts
    # from what is stored, and is stored by itself.
    setup.docdb.breaks_after = None
    socket.send(setup.gem.id, "hello again")
    await socket.until("chat.done")
    # The line that gave the time was not stored either, so the next message gets one.
    assert await setup.stored() == [
        TIME_LINE,
        {"role": "user", "content": "hello again"},
        {"role": "assistant", "content": "Hi again."},
    ]
    await _close(socket, linked)


@pytest.mark.parametrize(
    "frame",
    [
        "not json",
        json.dumps({"type": "chat.shout", "gem": "x", "text": "hi"}),
        json.dumps({"type": "chat.send", "gem": "x"}),
        json.dumps({"type": "chat.send", "gem": "x", "text": "   "}),
        json.dumps({"type": "chat.send", "gem": "x", "text": "a" * 20_001}),
        json.dumps({"text": "hi"}),
        json.dumps(["chat.send"]),
    ],
)
async def test_a_frame_that_cannot_be_read_gets_error_and_the_socket_stays_open(
    frame: str,
) -> None:
    # A binary frame is mic audio for a call, and is dropped without an answer when
    # there is none: that is in `test_link_call.py`.
    setup = await _chat(_said("Hi."))
    socket, linked = await _open(setup.service())

    socket.raw(frame)
    await socket.until("error")
    socket.send(setup.gem.id, "hello")
    await socket.until("chat.done")

    assert socket.frames[1]["type"] == "error"
    assert socket.frames[1]["text"]
    assert not linked.done()
    assert setup.llm.requests[0]["input"] == [TIME_LINE, {"role": "user", "content": "hello"}]
    await _close(socket, linked)


async def test_a_socket_that_closes_cuts_its_reply_which_is_stored() -> None:
    never = asyncio.Event()
    setup = await _chat([_delta("Once upon "), never])
    socket, linked = await _open(setup.service())

    socket.send(setup.gem.id, "a story")
    await socket.until("chat.text")
    await _close(socket, linked)

    # Stored by the time the socket's handler is done.
    assert await setup.stored() == [
        TIME_LINE,
        {"role": "user", "content": "a story"},
        {"role": "assistant", "content": "Once upon…"},
    ]


async def test_a_message_sent_while_the_store_is_slow_waits_for_it() -> None:
    setup = await _chat(_said("Hi."), _said("Again."))
    setup.docdb.slow = asyncio.Event()
    socket, linked = await _open(setup.service())

    socket.send(setup.gem.id, "hello")
    await socket.until("chat.text")
    socket.send(setup.gem.id, "hello again")
    await asyncio.sleep(0.02)
    # The first reply was written in full, and is still being stored.
    assert socket.chat() == [("chat.text", "Hi.")]
    assert len(setup.llm.requests) == 1

    setup.docdb.slow.set()
    await socket.until("chat.done", 2)
    assert socket.chat() == [
        ("chat.text", "Hi."),
        ("chat.done", "Hi."),
        ("chat.text", "Again."),
        ("chat.done", "Again."),
    ]
    stored = await setup.stored()
    assert stored[0] == TIME_LINE
    assert [item["content"] for item in stored[1:]] == [
        "hello",
        "Hi.",
        "hello again",
        "Again.",
    ]
    await _close(socket, linked)


async def test_a_changed_gem_applies_from_the_next_message() -> None:
    setup = await _chat(_said("Hi."), _said("Namaste."))
    socket, linked = await _open(setup.service())
    socket.send(setup.gem.id, "hello")
    await socket.until("chat.done")

    await setup.gems.update(
        str(setup.gem.id),
        {"name": "Gem Four", "instructions": "You keep the household accounts.", "tools": []},
    )
    socket.send(setup.gem.id, "hello again")
    await socket.until("chat.done", 2)

    first, second = setup.llm.requests
    assert (
        "You are Gem One.\n\nYou look after the user's personal finances.\n\n"
        in (first["instructions"])
    )
    assert second["instructions"] == _told("Gem Four", "You keep the household accounts.")
    assert _tool_names(first) == ["web_search", "cancel_tool_call"]
    assert _tool_names(second) == []
    # The conversation carries on as it was stored.
    assert second["input"][:3] == [
        TIME_LINE,
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi."},
    ]
    await _close(socket, linked)


async def test_a_gem_without_search_or_without_the_key_is_offered_no_tools() -> None:
    setup = await _chat(_said("Hi."), search=_Search(configured=False))
    socket, linked = await _open(setup.service())

    socket.send(setup.gem.id, "hello")
    await socket.until("chat.done")

    [request] = setup.llm.requests
    assert _tool_names(request) == []
    # What the gem is told does not depend on the tools it is offered.
    assert request["instructions"] == _told(
        "Gem One", "You look after the user's personal finances."
    )
    await _close(socket, linked)


# ---- the history route


async def test_the_history_is_shown_oldest_first_with_times_in_utc() -> None:
    setup = await _chat()
    await setup.store.write(
        [
            _item(setup.gem.id, 0, "what is my balance?"),
            _kept(
                setup.gem.id,
                1,
                {
                    "type": "function_call",
                    "call_id": "c",
                    "name": "web_search",
                    "arguments": "{}",
                },
                shown="current balance of the savings account",
            ),
            _kept(
                setup.gem.id,
                2,
                {"type": "function_call_output", "call_id": "c", "output": "40,000"},
            ),
            _kept(
                setup.gem.id,
                3,
                {"role": "assistant", "content": "It is forty thousand rupees."},
            ),
        ]
    )

    chat = await GemsServiceV1Class(setup.gems, setup.conversations).get_chat(str(setup.gem.id))

    assert json.loads(chat.model_dump_json()) == {
        "messages": [
            {"role": "user", "text": "what is my balance?", "at": "2026-11-17T09:00:00Z"},
            {
                "role": "tool",
                "text": "current balance of the savings account",
                "at": "2026-11-17T09:00:00Z",
            },
            {"role": "gem", "text": "It is forty thousand rupees.", "at": "2026-11-17T09:00:00Z"},
        ]
    }


@pytest.mark.parametrize("gem_id", [str(ObjectId()), "not-an-id"])
async def test_the_history_of_a_gem_that_is_not_there_is_a_404(gem_id: str) -> None:
    setup = await _chat()

    with pytest.raises(NotFoundError):
        await GemsServiceV1Class(setup.gems, setup.conversations).get_chat(gem_id)


async def test_a_deleted_gem_has_no_history() -> None:
    setup = await _chat(_said("Hi."))
    socket, linked = await _open(setup.service())
    socket.send(setup.gem.id, "hello")
    await socket.until("chat.done")
    await _close(socket, linked)

    await setup.gems.delete(str(setup.gem.id))

    assert _stored_items(setup.docdb) == []
    with pytest.raises(NotFoundError):
        await GemsServiceV1Class(setup.gems, setup.conversations).get_chat(str(setup.gem.id))


async def test_a_changed_darbar_text_applies_from_the_next_message() -> None:
    setup = await _chat(_said("Hi."), _said("Hi again."), _said("And again."))
    socket, linked = await _open(setup.service())
    darbar = DarbarEntityClass(cast("Any", setup.docdb))

    socket.send(setup.gem.id, "hello")
    await socket.until("chat.done")
    await darbar.set("Every gem answers to the user alone.")
    socket.send(setup.gem.id, "hello again")
    await socket.until("chat.done", 2)
    await darbar.set("")
    socket.send(setup.gem.id, "and again")
    await socket.until("chat.done", 3)

    first, second, third = (request["instructions"] for request in setup.llm.requests)
    # Until the user writes their own, it is the built-in text.
    assert first.startswith("The user keeps a few assistants of their own")
    assert second.startswith("Every gem answers to the user alone.\n\nYou are Gem One.\n")
    # An empty one tells the gem nothing there.
    assert third.startswith("You are Gem One.\n")
    await _close(socket, linked)


async def test_a_gem_that_lost_its_search_still_declares_it_while_its_history_holds_searches() -> (
    None
):
    call = _search_call("Pune weather")
    again = _search_call("Pune weather now", "call_2")
    setup = await _chat(
        [_delta("Let me look."), _starts(call), _completed(call)],
        _said("Sunny."),
        [_starts(again), _completed(again)],
        _said("I cannot look that up now."),
    )
    socket, linked = await _open(setup.service())
    socket.send(setup.gem.id, "weather?")
    await socket.until("chat.done")

    await setup.gems.update(str(setup.gem.id), {"tools": []})
    socket.send(setup.gem.id, "and now?")
    await socket.until("chat.done", 2)

    asked = setup.llm.requests[2]
    # A provider refuses calls of a tool it is not told of, so it is still told of it.
    assert _tool_names(asked) == ["web_search"]
    assert asked["input"][0] == TIME_LINE
    assert [item.get("type") for item in asked["input"][3:5]] == [
        "function_call",
        "function_call_output",
    ]
    # It is told what it was told before, and a search does not run.
    assert asked["instructions"] == setup.llm.requests[0]["instructions"]
    assert setup.search.queries == ["Pune weather"]
    refused = setup.llm.requests[3]["input"][-1]
    assert refused == {
        "type": "function_call_output",
        "call_id": "call_2",
        "output": "This tool is not available to you now.",
    }
    await _close(socket, linked)


async def test_a_write_that_stops_part_way_leaves_nothing_half_stored() -> None:
    call = _search_call("Pune weather")
    setup = await _chat(
        [_delta("Let me look."), _starts(call), _completed(call)], _said("Sunny."), _said("Hi.")
    )
    socket, linked = await _open(setup.service())

    # Every write of more than one entry stops after its first, as it would between a
    # call and its result. What it did put in is taken out again, and nothing that was
    # added after an entry that is not stored is stored ahead of it.
    setup.docdb.breaks_after = 1
    socket.send(setup.gem.id, "weather?")
    await socket.until("chat.error")
    assert socket.chat()[-1][1].startswith("the reply could not be stored")
    assert await setup.stored() == []

    setup.docdb.breaks_after = None
    socket.send(setup.gem.id, "hello")
    await socket.until("chat.done")
    assert await setup.stored() == [
        TIME_LINE,
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi."},
    ]
    await _close(socket, linked)


async def test_a_gem_deleted_while_its_reply_is_written_leaves_no_conversation() -> None:
    held = asyncio.Event()
    setup = await _chat([_delta("Once upon "), held, _delta("a time."), _completed()])
    socket, linked = await _open(setup.service())
    socket.send(setup.gem.id, "a story")
    await socket.until("chat.text")

    await setup.gems.delete(str(setup.gem.id))
    held.set()
    await socket.until("chat.done")

    assert _stored_items(setup.docdb) == []
    await _close(socket, linked)


# ---- a search that carries on after a cut, and its result told later


def _placeholder(call_id: str) -> str:
    return (
        f"Still running in the background, under call_id {call_id}. It has not failed. Only "
        "if the user says they no longer want this, stop it with cancel_tool_call and that "
        "call_id. Otherwise leave it alone: its result will reach you by itself, as a "
        f'separate call with "deferred_result_for": "{call_id}". Until then, do not make '
        "this call again, in any wording."
    )


_LOST = (
    "This call was lost before it finished, and its result will not arrive. Make it again "
    "if it is still needed."
)


class _Searches:
    """Web search, with a gate of its own for each query that is given one."""

    configured = True

    def __init__(self, *gated: str) -> None:
        self.gates = {query: asyncio.Event() for query in gated}
        self.queries: list[str] = []
        self.cancelled: list[str] = []
        self.finished: list[str] = []

    async def search(self, *, objective: str, queries: list[str], mode: str) -> Any:
        self.queries.append(objective)
        try:
            if (gate := self.gates.get(objective)) is not None:
                await gate.wait()
        except asyncio.CancelledError:
            self.cancelled.append(objective)
            raise
        self.finished.append(objective)
        page = SimpleNamespace(
            title="Weather", url="https://example.com", publish_date=None, excerpts=[objective]
        )
        return SimpleNamespace(results=[page])

    def found(self, query: str) -> str:
        """The result the model is given for `query`."""
        return f"Weather (https://example.com)\n{query}"


def _cancel_call(call_id: str, named: str) -> Any:
    return SimpleNamespace(
        type="function_call",
        call_id=call_id,
        name="cancel_tool_call",
        arguments=json.dumps({"call_id": named}),
    )


def _kinds(items: Sequence[Any]) -> list[str]:
    return [item.get("role") or item["type"] for item in items]


def _stub_of(item: Any) -> str:
    """The call a stub item delivers for."""
    assert item["type"] == "function_call"
    assert item["call_id"].startswith("late_")
    assert len(item["call_id"]) == len("late_") + 16
    return json.loads(item["arguments"])["deferred_result_for"]


def _thinks(setup: _Chat) -> list[bool]:
    """Whether each request so far asked the model to think a little."""
    return [request["reasoning"] == {"effort": "low"} for request in setup.llm.requests]


class _Grows:
    """Checks that the stored conversation is only ever added to."""

    def __init__(self, setup: _Chat) -> None:
        self._setup = setup
        self._seen: list[Any] = []

    async def check(self) -> list[Any]:
        now = await self._setup.stored()
        assert now[: len(self._seen)] == self._seen, "a stored item was changed or removed"
        self._seen = now
        return now


def _a_valid_chain(items: Sequence[Any]) -> None:
    """Every call has exactly one result, directly after the calls of its round."""
    open_calls: list[str] = []
    answered: set[str] = set()
    for item in items:
        kind = item.get("type")
        if kind == "function_call":
            open_calls.append(item["call_id"])
        elif kind == "function_call_output":
            assert item["call_id"] in open_calls, "a result with no call before it"
            assert item["call_id"] not in answered, "a call with two results"
            answered.add(item["call_id"])
        else:
            assert set(open_calls) <= answered, "a message between a call and its result"
    assert set(open_calls) == answered


async def _settle() -> None:
    """Let what is running now finish its steps."""
    await asyncio.sleep(0.05)


async def _until_stored(setup: _Chat, count: int) -> list[Any]:
    for _ in range(200):
        stored = await setup.stored()
        if len(stored) >= count:
            return stored
        await asyncio.sleep(0.01)
    raise AssertionError(f"fewer than {count} items were stored")


_WEATHER = "Pune weather"


def _looks() -> list[Any]:
    """A first reply that says a word and then searches for the weather."""
    call = _search_call(_WEATHER)
    return [_delta("Let me look."), _starts(call), _completed(call)]


async def _cut_while_searching(
    setup: _Chat, socket: _Socket, cutting: str = "and the time?"
) -> None:
    socket.send(setup.gem.id, "weather?")
    await socket.until("chat.tool")
    socket.send(setup.gem.id, cutting)
    await socket.until("chat.done")


async def test_a_message_that_cuts_a_reply_while_a_search_runs_leaves_the_search_running() -> None:
    searches = _Searches(_WEATHER)
    setup = await _chat(
        _looks(), _said("It is noon. The weather is still coming."), search=searches
    )
    grows = _Grows(setup)
    socket, linked = await _open(setup.service())

    await _cut_while_searching(setup, socket)

    assert socket.chat() == [
        ("chat.text", "Let me look."),
        ("chat.tool", _WEATHER),
        ("chat.cut", "Let me look."),
        ("chat.text", "It is noon. The weather is still coming."),
        ("chat.done", "It is noon. The weather is still coming."),
    ]
    assert searches.cancelled == [] and searches.finished == []
    stored = await grows.check()
    assert stored[0] == TIME_LINE
    assert _kinds(stored) == [
        "developer",
        "user",
        "assistant",
        "function_call",
        "function_call_output",
        "user",
        "assistant",
    ]
    assert stored[4] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": _placeholder("call_1"),
    }
    # The new message is answered first, from a history that ends that way.
    assert setup.llm.requests[1]["input"] == stored[:6]
    assert _thinks(setup) == [False, True]
    await _close(socket, linked)
    searches.gates[_WEATHER].set()


async def test_a_finished_search_is_stored_after_the_conversation_and_told_unasked() -> None:
    searches = _Searches(_WEATHER)
    setup = await _chat(
        _looks(), _said("Noon."), _said("It is sunny in Pune."), _said("Bye."), search=searches
    )
    grows = _Grows(setup)
    socket, linked = await _open(setup.service())
    await _cut_while_searching(setup, socket)
    await grows.check()

    searches.gates[_WEATHER].set()
    await socket.until("chat.done", 2)

    assert socket.chat()[-3:] == [
        ("chat.result", _WEATHER),
        ("chat.text", "It is sunny in Pune."),
        ("chat.done", "It is sunny in Pune."),
    ]
    assert socket.frames[-3] == {"type": "chat.result", "gem": str(setup.gem.id), "text": _WEATHER}
    stored = await grows.check()
    assert _kinds(stored[7:]) == ["function_call", "function_call_output", "assistant"]
    stub, result, told = stored[7:]
    assert _stub_of(stub) == "call_1"
    assert stub["name"] == "web_search"
    assert result == {
        "type": "function_call_output",
        "call_id": stub["call_id"],
        "output": searches.found(_WEATHER),
    }
    assert told == {"role": "assistant", "content": "It is sunny in Pune."}
    # The reply about it is asked for with no new message: the history ends in the result.
    assert setup.llm.requests[2]["input"] == stored[:9]
    assert _thinks(setup) == [False, True, False]
    _a_valid_chain(stored)
    # Neither the placeholder's call nor the stub is shown twice: the stub is not shown.
    assert await setup.shown() == [
        ("user", "weather?"),
        ("gem", "Let me look."),
        ("tool", _WEATHER),
        ("user", "and the time?"),
        ("gem", "Noon."),
        ("gem", "It is sunny in Pune."),
    ]

    # The next message carries on from it.
    socket.send(setup.gem.id, "thanks")
    await socket.until("chat.done", 3)
    assert setup.llm.requests[3]["input"] == [*stored, {"role": "user", "content": "thanks"}]
    await grows.check()
    await _close(socket, linked)


async def test_a_result_that_comes_while_a_reply_is_written_waits_for_it() -> None:
    searches = _Searches(_WEATHER)
    held = asyncio.Event()
    setup = await _chat(
        _looks(),
        [_delta("Once upon "), held, _delta("a time."), _completed()],
        _said("And it is sunny in Pune."),
        search=searches,
    )
    grows = _Grows(setup)
    socket, linked = await _open(setup.service())
    socket.send(setup.gem.id, "weather?")
    await socket.until("chat.tool")
    socket.send(setup.gem.id, "a story while we wait")
    await socket.until("chat.text", 2)

    searches.gates[_WEATHER].set()
    await _settle()
    # Not slipped into the reply in progress, and not told before it is stored.
    assert ("chat.result", _WEATHER) not in socket.chat()
    assert len(setup.llm.requests) == 2

    held.set()
    await socket.until("chat.done", 2)
    assert socket.chat()[-5:] == [
        ("chat.text", "a time."),
        ("chat.done", "Once upon a time."),
        ("chat.result", _WEATHER),
        ("chat.text", "And it is sunny in Pune."),
        ("chat.done", "And it is sunny in Pune."),
    ]
    stored = await grows.check()
    assert _kinds(stored[5:]) == [
        "user",
        "assistant",
        "function_call",
        "function_call_output",
        "assistant",
    ]
    assert _stub_of(stored[7]) == "call_1"
    assert setup.llm.requests[2]["input"] == stored[:9]
    await _close(socket, linked)


async def test_two_late_results_are_told_in_one_reply() -> None:
    searches = _Searches("Pune weather", "Delhi weather")
    pune, delhi = _search_call("Pune weather", "call_1"), _search_call("Delhi weather", "call_2")
    held = asyncio.Event()
    setup = await _chat(
        [_delta("Looking at both."), _starts(pune), _starts(delhi), _completed(pune, delhi)],
        [_delta("Sure, "), held, _delta("one moment."), _completed()],
        _said("Pune is sunny and Delhi is hazy."),
        search=searches,
    )
    grows = _Grows(setup)
    socket, linked = await _open(setup.service())
    socket.send(setup.gem.id, "weather in Pune and Delhi?")
    await socket.until("chat.tool", 2)
    socket.send(setup.gem.id, "hurry")
    await socket.until("chat.text", 2)

    searches.gates["Delhi weather"].set()
    searches.gates["Pune weather"].set()
    await _settle()
    held.set()
    await socket.until("chat.done", 2)

    assert socket.chat()[-4:] == [
        ("chat.result", "Delhi weather"),
        ("chat.result", "Pune weather"),
        ("chat.text", "Pune is sunny and Delhi is hazy."),
        ("chat.done", "Pune is sunny and Delhi is hazy."),
    ]
    stored = await grows.check()
    assert _kinds(stored[9:]) == [
        "function_call",
        "function_call_output",
        "function_call",
        "function_call_output",
        "assistant",
    ]
    assert [_stub_of(stored[9]), _stub_of(stored[11])] == ["call_2", "call_1"]
    assert len(setup.llm.requests) == 3
    assert setup.llm.requests[2]["input"] == stored[:13]
    _a_valid_chain(stored)
    await _close(socket, linked)


async def test_with_the_latest_sender_gone_the_result_is_only_stored() -> None:
    searches = _Searches(_WEATHER)
    setup = await _chat(_looks(), _said("Noon."), _said("Also, it is sunny."), search=searches)
    grows = _Grows(setup)
    socket, linked = await _open(setup.service())
    await _cut_while_searching(setup, socket)
    await _close(socket, linked)

    searches.gates[_WEATHER].set()
    stored = await _until_stored(setup, 9)
    await _settle()

    await grows.check()
    assert _kinds(stored[7:]) == ["function_call", "function_call_output"]
    assert _stub_of(stored[7]) == "call_1"
    # No reply was written: nobody was there to read it.
    assert len(setup.llm.requests) == 2

    other, other_linked = await _open(setup.service())
    other.send(setup.gem.id, "so?")
    await other.until("chat.done")
    assert setup.llm.requests[2]["input"] == [*stored, {"role": "user", "content": "so?"}]
    assert other.chat() == [
        ("chat.text", "Also, it is sunny."),
        ("chat.done", "Also, it is sunny."),
    ]
    await grows.check()
    await _close(other, other_linked)


async def test_a_socket_that_closes_while_a_search_runs_leaves_it_running() -> None:
    searches = _Searches(_WEATHER)
    setup = await _chat(_looks(), search=searches)
    grows = _Grows(setup)
    socket, linked = await _open(setup.service())
    socket.send(setup.gem.id, "weather?")
    await socket.until("chat.tool")
    await _close(socket, linked)

    stored = await grows.check()
    assert stored[0] == TIME_LINE
    assert _kinds(stored) == [
        "developer",
        "user",
        "assistant",
        "function_call",
        "function_call_output",
    ]
    assert stored[4]["output"] == _placeholder("call_1")
    assert searches.cancelled == []

    searches.gates[_WEATHER].set()
    stored = await _until_stored(setup, 7)
    await grows.check()
    assert _stub_of(stored[5]) == "call_1"
    assert stored[6]["output"] == searches.found(_WEATHER)
    assert len(setup.llm.requests) == 1


async def test_a_new_message_cuts_the_reply_about_a_late_result() -> None:
    searches = _Searches(_WEATHER)
    never = asyncio.Event()
    setup = await _chat(
        _looks(),
        _said("Noon."),
        [_delta("It is sunny "), never],
        _said("You are welcome."),
        search=searches,
    )
    grows = _Grows(setup)
    socket, linked = await _open(setup.service())
    await _cut_while_searching(setup, socket)

    searches.gates[_WEATHER].set()
    await socket.until("chat.text", 3)
    socket.send(setup.gem.id, "thanks")
    await socket.until("chat.done", 2)

    assert socket.chat()[-5:] == [
        ("chat.result", _WEATHER),
        ("chat.text", "It is sunny "),
        ("chat.cut", "It is sunny…"),
        ("chat.text", "You are welcome."),
        ("chat.done", "You are welcome."),
    ]
    stored = await grows.check()
    assert _kinds(stored[7:]) == [
        "function_call",
        "function_call_output",
        "assistant",
        "user",
        "assistant",
    ]
    assert stored[9] == {"role": "assistant", "content": "It is sunny…"}
    # The new message waited until the cut reply was stored.
    assert setup.llm.requests[3]["input"] == stored[:11]
    await _close(socket, linked)


async def test_a_message_that_comes_while_the_result_is_stored_is_answered_with_it() -> None:
    searches = _Searches(_WEATHER)
    setup = await _chat(_looks(), _said("Noon."), _said("Hello. And it is sunny."), search=searches)
    grows = _Grows(setup)
    socket, linked = await _open(setup.service())
    await _cut_while_searching(setup, socket)

    # The result comes in, and is being stored when a message arrives.
    setup.docdb.slow = asyncio.Event()
    searches.gates[_WEATHER].set()
    await _settle()
    socket.send(setup.gem.id, "hello")
    await _settle()
    assert len(setup.llm.requests) == 2
    setup.docdb.slow.set()
    await socket.until("chat.done", 2)
    await _settle()

    stored = await grows.check()
    # The result is stored first, and only one reply is written: the one to the
    # message, made with the result in front of the model.
    assert len(setup.llm.requests) == 3
    assert ("chat.result", _WEATHER) not in socket.chat()
    assert socket.chat()[-2:] == [
        ("chat.text", "Hello. And it is sunny."),
        ("chat.done", "Hello. And it is sunny."),
    ]
    assert _kinds(stored[7:]) == ["function_call", "function_call_output", "user", "assistant"]
    assert setup.llm.requests[2]["input"] == stored[:10]
    await _close(socket, linked)


async def test_a_message_that_cuts_a_reply_while_a_result_waits_is_answered_with_it() -> None:
    searches = _Searches(_WEATHER)
    held = asyncio.Event()
    setup = await _chat(
        _looks(),
        [_delta("Once upon "), held, _delta("a time."), _completed()],
        _said("Sure. And it is sunny in Pune."),
        _said("You are welcome."),
        search=searches,
    )
    grows = _Grows(setup)
    socket, linked = await _open(setup.service())
    socket.send(setup.gem.id, "weather?")
    await socket.until("chat.tool")
    socket.send(setup.gem.id, "a story while we wait")
    await socket.until("chat.text", 2)
    # The search finishes while the story is written, so its result waits. Then a new
    # message cuts the story.
    searches.gates[_WEATHER].set()
    await _settle()
    socket.send(setup.gem.id, "stop, the weather?")
    await socket.until("chat.done")
    await _settle()

    # One reply, to the message, tells the result: no `chat.result`, no reply unasked.
    assert socket.chat()[-3:] == [
        ("chat.cut", "Once upon…"),
        ("chat.text", "Sure. And it is sunny in Pune."),
        ("chat.done", "Sure. And it is sunny in Pune."),
    ]
    assert ("chat.result", _WEATHER) not in socket.chat()
    assert len(setup.llm.requests) == 3
    # The stub and the result are stored ahead of the message, and the model was sent
    # them so, with nothing left without a result: no thinking.
    stored = await grows.check()
    assert _kinds(stored[5:]) == [
        "user",
        "assistant",
        "function_call",
        "function_call_output",
        "user",
        "assistant",
    ]
    assert stored[6] == {"role": "assistant", "content": "Once upon…"}
    assert _stub_of(stored[7]) == "call_1"
    assert stored[8]["output"] == searches.found(_WEATHER)
    assert stored[9] == {"role": "user", "content": "stop, the weather?"}
    assert setup.llm.requests[2]["input"] == stored[:10]
    assert _thinks(setup) == [False, True, False]
    _a_valid_chain(stored)

    # The result was taken, not lost: the next message closes nothing first.
    socket.send(setup.gem.id, "thanks")
    await socket.until("chat.done", 2)
    assert setup.llm.requests[3]["input"] == [*stored, {"role": "user", "content": "thanks"}]
    await grows.check()
    await _close(socket, linked)


async def test_a_message_left_open_is_closed_ahead_of_a_result_told_with_the_next() -> None:
    searches = _Searches(_WEATHER)
    never = asyncio.Event()
    setup = await _chat(
        _looks(),
        [never, _completed()],
        _said("Hi. It is sunny in Pune."),
        search=searches,
    )
    grows = _Grows(setup)
    socket, linked = await _open(setup.service())
    socket.send(setup.gem.id, "weather?")
    await socket.until("chat.tool")
    socket.send(setup.gem.id, "hello")
    await _eventually_asked(setup, 2)
    # The result comes in while "hello" is being answered, and a new message cuts that
    # reply before any of it was sent: "hello" is still open, and was sent to the model.
    searches.gates[_WEATHER].set()
    await _settle()
    socket.send(setup.gem.id, "anyone there?")
    await socket.until("chat.done")
    await _settle()

    stored = await grows.check()
    assert stored[0] == TIME_LINE
    assert _kinds(stored) == [
        "developer",
        "user",
        "assistant",
        "function_call",
        "function_call_output",
        "user",
        "function_call",
        "function_call_output",
        "user",
        "assistant",
    ]
    assert stored[5] == {"role": "user", "content": "hello"}
    assert _stub_of(stored[6]) == "call_1"
    assert stored[8] == {"role": "user", "content": "anyone there?"}
    assert setup.llm.requests[2]["input"] == stored[:9]
    assert len(setup.llm.requests) == 3
    _a_valid_chain(stored)
    await _close(socket, linked)


async def test_cancel_tool_call_stops_a_search_still_running() -> None:
    searches = _Searches(_WEATHER)
    setup = await _chat(
        _looks(),
        [_starts(_cancel_call("call_9", "call_1")), _completed(_cancel_call("call_9", "call_1"))],
        _said("Okay, I stopped it."),
        search=searches,
    )
    grows = _Grows(setup)
    socket, linked = await _open(setup.service())

    await _cut_while_searching(setup, socket, "never mind the weather")

    await _settle()
    assert searches.cancelled == [_WEATHER]
    stored = await grows.check()
    assert stored[6:8] == [
        {
            "type": "function_call",
            "call_id": "call_9",
            "name": "cancel_tool_call",
            "arguments": json.dumps({"call_id": "call_1"}),
        },
        {"type": "function_call_output", "call_id": "call_9", "output": "Cancelled."},
    ]
    # Thinking stops with it, nothing is told later, and the cancel is not shown.
    assert _thinks(setup) == [False, True, False]
    assert len(setup.llm.requests) == 3
    assert ("chat.tool", "") not in socket.chat()
    assert await setup.shown() == [
        ("user", "weather?"),
        ("gem", "Let me look."),
        ("tool", _WEATHER),
        ("user", "never mind the weather"),
        ("gem", "Okay, I stopped it."),
    ]
    await _close(socket, linked)


async def test_cancel_tool_call_drops_a_result_that_has_not_been_told() -> None:
    searches = _Searches(_WEATHER)
    held = asyncio.Event()
    cancel = _cancel_call("call_9", "call_1")
    setup = await _chat(
        _looks(),
        [held, _starts(cancel), _completed(cancel)],
        _said("Okay."),
        search=searches,
    )
    grows = _Grows(setup)
    socket, linked = await _open(setup.service())
    socket.send(setup.gem.id, "weather?")
    await socket.until("chat.tool")
    socket.send(setup.gem.id, "never mind the weather")
    await _settle()

    # The search finishes while the reply that will cancel it is being written.
    searches.gates[_WEATHER].set()
    await _settle()
    held.set()
    await socket.until("chat.done")
    await _settle()

    stored = await grows.check()
    assert stored[7] == {
        "type": "function_call_output",
        "call_id": "call_9",
        "output": "Cancelled.",
    }
    assert ("chat.result", _WEATHER) not in socket.chat()
    assert not any(item.get("call_id", "").startswith("late_") for item in stored)
    assert len(setup.llm.requests) == 3
    # It was waiting, with no result in the history, so the model thought until it went.
    assert _thinks(setup) == [False, True, False]
    await _close(socket, linked)


@pytest.mark.parametrize("named", ["stub", "call_1", "call_unknown"])
async def test_cancel_tool_call_with_nothing_running_says_so(named: str) -> None:
    searches = _Searches(_WEATHER)
    setup = await _chat(_looks(), _said("Noon."), _said("It is sunny."), search=searches)
    socket, linked = await _open(setup.service())
    await _cut_while_searching(setup, socket)
    searches.gates[_WEATHER].set()
    await socket.until("chat.done", 2)
    stub_id = (await setup.stored())[7]["call_id"]
    target = stub_id if named == "stub" else named
    cancel = _cancel_call("call_9", target)
    setup.llm.then([_starts(cancel), _completed(cancel)], _said("It had already come."))

    socket.send(setup.gem.id, "stop that search")
    await socket.until("chat.done", 3)

    assert (await setup.stored())[-2] == {
        "type": "function_call_output",
        "call_id": "call_9",
        "output": "Nothing is running under that call_id.",
    }
    await _close(socket, linked)


async def test_thinking_lasts_while_a_search_has_no_result_in_the_history() -> None:
    searches = _Searches("Pune weather", "Pune time")
    time_call = _search_call("Pune time", "call_2")
    setup = await _chat(
        _looks(),
        [_starts(time_call), _completed(time_call)],
        _said("It is noon."),
        _said("And it is sunny."),
        search=searches,
    )
    socket, linked = await _open(setup.service())
    socket.send(setup.gem.id, "weather?")
    await socket.until("chat.tool")
    socket.send(setup.gem.id, "and the time?")
    await socket.until("chat.tool", 2)

    # The weather comes in while the time is looked up: it waits, still not in the history.
    searches.gates["Pune weather"].set()
    await _settle()
    searches.gates["Pune time"].set()
    await socket.until("chat.done", 2)

    # Before any search; while the weather ran; while it waited; once it was told.
    assert _thinks(setup) == [False, True, True, False]
    await _close(socket, linked)


async def test_a_search_lost_to_a_restart_is_closed_once_before_the_next_message() -> None:
    searches = _Searches(_WEATHER)
    setup = await _chat(_looks(), _said("Noon."), search=searches)
    socket, linked = await _open(setup.service())
    await _cut_while_searching(setup, socket)
    await _close(socket, linked)
    # The old process is gone: its search never comes back (its gate is never opened).

    later = setup.again()
    grows = _Grows(later)
    before = await grows.check()
    setup.llm.then(_said("That search was lost. Shall I look again?"), _said("Okay."))
    socket, linked = await _open(later.service())
    socket.send(later.gem.id, "so, the weather?")
    await socket.until("chat.done")

    stored = await grows.check()
    assert _kinds(stored[7:]) == ["function_call", "function_call_output", "user", "assistant"]
    assert _stub_of(stored[7]) == "call_1"
    assert stored[7]["name"] == "web_search"
    assert stored[8] == {
        "type": "function_call_output",
        "call_id": stored[7]["call_id"],
        "output": _LOST,
    }
    assert setup.llm.requests[2]["input"] == [
        *before,
        stored[7],
        stored[8],
        {"role": "user", "content": "so, the weather?"},
    ]
    # Nothing is pending any more, and the stub is not shown.
    assert _thinks(setup)[2] is False
    assert ("gem", "That search was lost. Shall I look again?") in await later.shown()
    assert len(await later.shown()) == 7

    socket.send(later.gem.id, "no")
    await socket.until("chat.done", 2)
    stored = await grows.check()
    stubs = [
        item
        for item in stored
        if item.get("type") == "function_call" and "late_" in item["call_id"]
    ]
    assert len(stubs) == 1
    await _close(socket, linked)


async def test_a_result_that_could_not_be_stored_is_closed_as_lost_before_the_next_one() -> None:
    searches = _Searches("Pune weather", "Delhi weather")
    pune, delhi = _search_call("Pune weather", "call_1"), _search_call("Delhi weather", "call_2")
    setup = await _chat(
        [_delta("Looking at both."), _starts(pune), _starts(delhi), _completed(pune, delhi)],
        _said("Sure."),
        _said("Delhi is hazy. The Pune search was lost."),
        search=searches,
    )
    grows = _Grows(setup)
    socket, linked = await _open(setup.service())
    socket.send(setup.gem.id, "weather in Pune and Delhi?")
    await socket.until("chat.tool", 2)
    socket.send(setup.gem.id, "hurry")
    await socket.until("chat.done")

    # Pune's result cannot be stored: the database is out of reach.
    setup.docdb.breaks_after = 0
    searches.gates["Pune weather"].set()
    await _settle()
    setup.docdb.breaks_after = None
    assert len(await grows.check()) == 9

    searches.gates["Delhi weather"].set()
    await socket.until("chat.done", 2)

    stored = await grows.check()
    assert _kinds(stored[9:]) == [
        "function_call",
        "function_call_output",
        "function_call",
        "function_call_output",
        "assistant",
    ]
    assert _stub_of(stored[9]) == "call_1" and stored[10]["output"] == _LOST
    assert _stub_of(stored[11]) == "call_2"
    assert stored[12]["output"] == searches.found("Delhi weather")
    assert [frame for frame in socket.chat() if frame[0] == "chat.result"] == [
        ("chat.result", "Delhi weather")
    ]
    _a_valid_chain(stored)
    await _close(socket, linked)


@pytest.mark.parametrize("how", ["cancelled", "delivered"])
async def test_a_search_that_was_cancelled_or_told_is_not_lost(how: str) -> None:
    searches = _Searches(_WEATHER)
    cancel = _cancel_call("call_9", "call_1")
    setup = await _chat(
        _looks(),
        *(
            [[_starts(cancel), _completed(cancel)], _said("Stopped.")]
            if how == "cancelled"
            else [_said("Noon."), _said("It is sunny.")]
        ),
        search=searches,
    )
    socket, linked = await _open(setup.service())
    await _cut_while_searching(setup, socket)
    if how == "delivered":
        searches.gates[_WEATHER].set()
        await socket.until("chat.done", 2)
    await _close(socket, linked)
    before = await setup.stored()

    later = setup.again()
    setup.llm.then(_said("Hi."))
    socket, linked = await _open(later.service())
    socket.send(later.gem.id, "hello")
    await socket.until("chat.done")

    assert await later.stored() == [
        *before,
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi."},
    ]
    await _close(socket, linked)


@pytest.mark.parametrize("how", ["lost", "cancelled"])
async def test_rewording_a_result_does_not_change_which_stored_calls_are_lost(
    how: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # What is stored is read by the marks stored with it, never by its wording.
    searches = _Searches(_WEATHER)
    cancel = _cancel_call("call_9", "call_1")
    setup = await _chat(
        _looks(),
        *([[_starts(cancel), _completed(cancel)], _said("Stopped.")] if how == "cancelled" else []),
        *([_said("Noon.")] if how == "lost" else []),
        search=searches,
    )
    socket, linked = await _open(setup.service())
    await _cut_while_searching(setup, socket)
    await _close(socket, linked)
    before = await setup.stored()

    # The wording of the result each case turns on is changed, wherever it is: in its own
    # module, and in any that imports it.
    name, reworded = (
        ("PLACEHOLDER", "Reworded: {call_id} is still going.")
        if how == "lost"
        else ("CANCELLED", "Reworded: stopped.")
    )
    for module in (late_results, chat_ent, conversation_ent):
        monkeypatch.setattr(module, name, reworded, False)
    later = setup.again()
    setup.llm.then(_said("Hi."))
    socket, linked = await _open(later.service())
    socket.send(later.gem.id, "hello")
    await socket.until("chat.done")

    added = (await later.stored())[len(before) :]
    if how == "lost":
        assert _kinds(added) == ["function_call", "function_call_output", "user", "assistant"]
        assert _stub_of(added[0]) == "call_1" and added[1]["output"] == _LOST
    else:
        assert _kinds(added) == ["user", "assistant"]
    await _close(socket, linked)


async def test_a_reply_about_a_result_that_fails_to_begin_ends_in_chat_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    searches = _Searches(_WEATHER)
    setup = await _chat(_looks(), _said("Noon."), search=searches)
    socket, linked = await _open(setup.service())
    await _cut_while_searching(setup, socket)

    class _Gone:
        async def read(self) -> None:
            raise ConnectionError("the database went away")

    # The Darbar text cannot be read: the reply fails after `chat.result` was sent.
    monkeypatch.setattr(setup.chat, "_darbar", _Gone())
    searches.gates[_WEATHER].set()
    await socket.until("chat.error")

    assert socket.chat()[-2:] == [
        ("chat.result", _WEATHER),
        ("chat.error", "no reply could be made: the database went away"),
    ]
    # The result is stored all the same.
    assert _stub_of((await setup.stored())[7]) == "call_1"
    await _close(socket, linked)


async def test_a_deleted_gems_late_result_is_dropped() -> None:
    searches = _Searches(_WEATHER)
    setup = await _chat(_looks(), _said("Noon."), search=searches)
    socket, linked = await _open(setup.service())
    await _cut_while_searching(setup, socket)

    await setup.gems.delete(str(setup.gem.id))
    searches.gates[_WEATHER].set()
    await _settle()

    assert _stored_items(setup.docdb) == []
    assert ("chat.result", _WEATHER) not in socket.chat()
    assert len(setup.llm.requests) == 2
    await _close(socket, linked)


async def test_a_gem_that_lost_its_search_still_declares_cancel_tool_call_if_it_used_it() -> None:
    searches = _Searches(_WEATHER)
    cancel = _cancel_call("call_9", "call_1")
    setup = await _chat(
        _looks(),
        [_starts(cancel), _completed(cancel)],
        _said("Stopped."),
        _said("Hi."),
        search=searches,
    )
    socket, linked = await _open(setup.service())
    await _cut_while_searching(setup, socket)

    await setup.gems.update(str(setup.gem.id), {"tools": []})
    socket.send(setup.gem.id, "hello")
    await socket.until("chat.done", 2)

    asked = setup.llm.requests[-1]
    assert _tool_names(asked) == ["web_search", "cancel_tool_call"]
    assert asked["instructions"] == setup.llm.requests[0]["instructions"]
    await _close(socket, linked)


# ---- the same on an Anthropic model


class _KeyedClaude(_Claude):
    configured = True


async def test_placeholder_stub_and_result_make_a_valid_anthropic_history() -> None:
    searches = _Searches(_WEATHER)
    claude = _KeyedClaude(
        [_text("Let me look."), _call_starts(), _final(("toolu_1", _WEATHER))],
        [_text("Noon."), _final()],
        [_text("It is sunny."), _final()],
        [_text("Hi."), _final()],
    )
    setup = await _chat(model="Haiku 5.5", search=searches)
    setup = setup.again(openai=_NoKey(), anthropic=claude, search=searches)
    socket, linked = await _open(setup.service())
    await _cut_while_searching(setup, socket)
    searches.gates[_WEATHER].set()
    await socket.until("chat.done", 2)
    # And a search lost to a restart, closed before a message.
    await setup.store.write(
        [
            _kept(setup.gem.id, seq, item, mark=mark)
            for seq, (item, mark) in zip(
                # The next numbers of the gem's log, as the log itself hands them out.
                setup.conversations.log.take(setup.gem.id, 3),
                [
                    ({"role": "user", "content": "and Delhi?"}, None),
                    (
                        {
                            "type": "function_call",
                            "call_id": "toolu_2",
                            "name": "web_search",
                            "arguments": json.dumps({"query": "Delhi weather"}),
                        },
                        None,
                    ),
                    (
                        {
                            "type": "function_call_output",
                            "call_id": "toolu_2",
                            "output": _placeholder("toolu_2"),
                        },
                        "placeholder",
                    ),
                ],
                strict=True,
            )
        ]
    )
    socket.send(setup.gem.id, "hello")
    await socket.until("chat.done", 3)

    stored = await setup.stored()
    assert stored[0] == TIME_LINE
    assert _kinds(stored) == [
        "developer",
        "user",
        "assistant",
        "function_call",
        "function_call_output",
        "user",
        "assistant",
        "function_call",
        "function_call_output",
        "assistant",
        "user",
        "function_call",
        "function_call_output",
        "function_call",
        "function_call_output",
        "user",
        "assistant",
    ]
    assert stored[4]["output"] == _placeholder("toolu_1")
    assert _stub_of(stored[7]) == "toolu_1"
    assert _stub_of(stored[13]) == "toolu_2" and stored[14]["output"] == _LOST
    _a_valid_chain(stored)
    for request in claude.requests:
        _a_valid_anthropic_request(request["messages"])
    # The reply about the result was asked for with the result last, on the user side.
    told = claude.requests[2]["messages"][-1]
    assert told["role"] == "user"
    assert [block["type"] for block in told["content"]] == ["tool_result"]
    await _close(socket, linked)


async def test_a_result_told_with_a_message_makes_a_valid_anthropic_history() -> None:
    searches = _Searches(_WEATHER)
    held = asyncio.Event()
    claude = _KeyedClaude(
        [_text("Let me look."), _call_starts(), _final(("toolu_1", _WEATHER))],
        [_text("Once upon "), held, _text("a time."), _final()],
        [_text("Sure. It is sunny."), _final()],
    )
    setup = await _chat(model="Haiku 5.5", search=searches)
    setup = setup.again(openai=_NoKey(), anthropic=claude, search=searches)
    socket, linked = await _open(setup.service())
    socket.send(setup.gem.id, "weather?")
    await socket.until("chat.tool")
    socket.send(setup.gem.id, "a story while we wait")
    await socket.until("chat.text", 2)
    searches.gates[_WEATHER].set()
    await _settle()
    socket.send(setup.gem.id, "stop, the weather?")
    await socket.until("chat.done")

    stored = await setup.stored()
    assert _kinds(stored[-4:]) == ["function_call", "function_call_output", "user", "assistant"]
    _a_valid_chain(stored)
    for request in claude.requests:
        _a_valid_anthropic_request(request["messages"])
    # The result and the message are on the user's side together, the result first.
    asked = claude.requests[2]["messages"][-1]
    assert asked["role"] == "user"
    assert [block["type"] for block in asked["content"]] == ["tool_result", "text"]
    assert asked["content"][1]["text"] == "stop, the weather?"
    await _close(socket, linked)


def _a_valid_anthropic_request(messages: list[Any]) -> None:
    """Alternating sides, opening and closing on the user's, each tool use answered next."""
    assert [m["role"] for m in messages] == ["user", "assistant"] * (len(messages) // 2) + ["user"]
    for index, message in enumerate(messages):
        used = [b["id"] for b in message["content"] if b["type"] == "tool_use"]
        if used:
            answer = messages[index + 1]["content"]
            assert [b["tool_use_id"] for b in answer[: len(used)]] == used


# ---- found in review


class _SetsThenGoes(asyncio.Event):
    """A step of a scripted reply that sets another event and does not wait."""

    def __init__(self, other: asyncio.Event) -> None:
        super().__init__()
        self._other = other

    async def wait(self) -> bool:
        self._other.set()
        return True


async def test_a_search_cancelled_in_the_instant_it_finishes_is_not_told() -> None:
    # The search finishes in the same step the gem cancels it in, too late to be
    # stopped. The gem has been told "Cancelled.", so its result is dropped all the same.
    finish = asyncio.Event()

    class _FinishesWhenTold(_Searches):
        async def search(self, *, objective: str, queries: list[str], mode: str) -> Any:
            await finish.wait()
            return await super().search(objective=objective, queries=queries, mode=mode)

    searches = _FinishesWhenTold()
    cancel = _cancel_call("call_9", "call_1")
    setup = await _chat(
        _looks(),
        [_SetsThenGoes(finish), _starts(cancel), _completed(cancel)],
        _said("Okay, I stopped it."),
        _said("By the way, it is sunny in Pune."),
        search=searches,
    )
    grows = _Grows(setup)
    socket, linked = await _open(setup.service())

    await _cut_while_searching(setup, socket, "never mind the weather")
    await _settle()

    assert searches.finished == [_WEATHER] and searches.cancelled == []
    assert socket.chat()[-1] == ("chat.done", "Okay, I stopped it.")
    assert "chat.result" not in [type_ for type_, _ in socket.chat()]
    stored = await grows.check()
    _a_valid_chain(stored)
    assert stored[7] == {
        "type": "function_call_output",
        "call_id": "call_9",
        "output": "Cancelled.",
    }
    assert not any(item.get("call_id", "").startswith("late_") for item in stored)
    assert len(setup.llm.requests) == 3
    await _close(socket, linked)


class _FlakyGems:
    """The gems, with `exists` failing as many times as `fails` says."""

    def __init__(self, gems: GemEntityClass) -> None:
        self._gems = gems
        self.fails = 0

    async def get(self, gem_id: str) -> GemConfig:
        return await self._gems.get(gem_id)

    def when_deleted(self, told: Any) -> None:
        self._gems.when_deleted(told)

    async def exists(self, gem_id: str) -> bool:
        if self.fails:
            self.fails -= 1
            raise ConnectionError("the database blinked")
        return await self._gems.exists(gem_id)


async def test_a_search_left_running_is_kept_track_of_even_if_the_next_step_fails() -> None:
    # Its placeholder is stored by then, so its result has to be taken when it comes:
    # not thrown away, and not written off as lost.
    searches = _Searches(_WEATHER)
    setup = await _chat(_looks(), _said("Noon."), _said("Also, it is sunny."), search=searches)
    gems = _FlakyGems(setup.gems)
    setup = setup.again(gems=gems)
    grows = _Grows(setup)
    socket, linked = await _open(setup.service())
    socket.send(setup.gem.id, "weather?")
    await socket.until("chat.tool")

    gems.fails = 1  # the look that follows storing the cut reply
    socket.send(setup.gem.id, "and the time?")
    await socket.until("chat.done")
    searches.gates[_WEATHER].set()
    await socket.until("chat.done", 2)

    stored = await grows.check()
    _a_valid_chain(stored)
    assert not any(item.get("output") == _LOST for item in stored)
    assert _stub_of(stored[7]) == "call_1"
    assert stored[8]["output"] == searches.found(_WEATHER)
    assert socket.chat()[-3:] == [
        ("chat.result", _WEATHER),
        ("chat.text", "Also, it is sunny."),
        ("chat.done", "Also, it is sunny."),
    ]
    await _close(socket, linked)


async def test_a_reply_about_a_late_result_is_cut_when_its_client_goes() -> None:
    # Nobody sent a message for it, so the socket holds no turn to cut: chat is told the
    # client has gone, and cuts it. It is stored as what that client was sent.
    searches = _Searches(_WEATHER)
    held = asyncio.Event()
    setup = await _chat(
        _looks(),
        _said("Noon."),
        [_delta("It is "), held, _delta("sunny in Pune."), _completed()],
        search=searches,
    )
    grows = _Grows(setup)
    socket, linked = await _open(setup.service())
    await _cut_while_searching(setup, socket)
    searches.gates[_WEATHER].set()
    await socket.until("chat.text", 3)

    await _close(socket, linked)
    stored = await _until_stored(setup, 10)

    await grows.check()
    assert stored[9] == {"role": "assistant", "content": "It is…"}
    # The gem is free again, though the reply was never let finish.
    assert not held.is_set()
    other, other_linked = await _open(setup.again().service())
    await _close(other, other_linked)


async def test_nothing_is_kept_for_an_id_that_names_no_gem_or_a_client_that_has_gone() -> None:
    setup = await _chat(_said("Hi."))
    socket, linked = await _open(setup.service())

    for _ in range(3):
        socket.send(ObjectId(), "hello")
    socket.send("not-an-id", "hello")
    await socket.until("chat.error", 4)
    assert setup.conversations._kept == {}
    assert setup.chat._latest == {}

    socket.send(setup.gem.id, "hello")
    await socket.until("chat.done")
    assert setup.conversations._kept == {}
    assert list(setup.chat._latest) == [str(setup.gem.id)]
    await _close(socket, linked)
    assert setup.chat._latest == {}


async def test_a_search_running_when_its_reply_cannot_be_stored_is_stopped() -> None:
    # Its call is in no stored conversation, so nothing could take its result.
    searches = _Searches(_WEATHER)
    setup = await _chat(_looks(), search=searches)
    socket, linked = await _open(setup.service())
    # The database takes nothing.
    setup.docdb.breaks_after = 0
    socket.send(setup.gem.id, "weather?")
    await socket.until("chat.tool")

    await _close(socket, linked)
    await _settle()

    assert searches.cancelled == [_WEATHER]
    assert _stored_items(setup.docdb) == []
    assert setup.conversations._kept == {}


async def test_a_search_running_when_its_gem_is_deleted_is_stopped() -> None:
    searches = _Searches(_WEATHER)
    setup = await _chat(_looks(), search=searches)
    socket, linked = await _open(setup.service())
    socket.send(setup.gem.id, "weather?")
    await socket.until("chat.tool")

    await setup.gems.delete(str(setup.gem.id))
    await _close(socket, linked)
    await _settle()

    assert searches.cancelled == [_WEATHER]
    assert _stored_items(setup.docdb) == []
    assert setup.conversations._kept == {}


async def test_a_client_that_goes_before_its_message_is_answered_leaves_it_stored() -> None:
    setup = await _chat(_said("Hi."))
    socket, linked = await _open(setup.service())

    socket.send(setup.gem.id, "hello")
    await _close(socket, linked)
    await _settle()

    # No reply got through, yet the message was sent: it is stored as it stands, and
    # nothing of it is kept in memory. That client is not written to again.
    assert await setup.stored() == [TIME_LINE, {"role": "user", "content": "hello"}]
    assert setup.conversations._kept == {}
    assert setup.chat._latest == {}
    other, other_linked = await _open(setup.service())
    other.send(setup.gem.id, "are you there?")
    await other.until("chat.done")
    assert await setup.stored() == [
        TIME_LINE,
        {"role": "user", "content": "hello"},
        {"role": "user", "content": "are you there?"},
        {"role": "assistant", "content": "Hi."},
    ]
    await _close(other, other_linked)
    assert setup.conversations._kept == {}


# ---- resetting a gem


async def _reset(setup: _Chat, gem_id: str | None = None) -> None:
    await setup.conversations.reset(str(setup.gem.id) if gem_id is None else gem_id)


async def test_a_reset_gem_starts_again_and_its_log_keeps_everything() -> None:
    setup = await _chat(_said("Hi."), _said("Hello."))
    socket, linked = await _open(setup.service())
    socket.send(setup.gem.id, "hello")
    await socket.until("chat.done")
    before = await setup.stored()

    await _reset(setup)

    assert await setup.stored() == []
    assert await setup.shown() == []
    socket.send(setup.gem.id, "who am I?")
    await socket.until("chat.done", 2)
    # Nothing from before is sent, and the time is given again, as to a new gem.
    assert setup.llm.requests[1]["input"] == [TIME_LINE, {"role": "user", "content": "who am I?"}]
    assert await setup.shown() == [("user", "who am I?"), ("gem", "Hello.")]
    # The log keeps all of it, with the reset in between.
    gem_id = str(setup.gem.id)
    entries, _ = await setup.conversations.log_page(gem_id)
    kinds = [entry["kind"] for entry in entries]
    assert kinds.count("gem.reset") == 1
    reset = kinds.index("gem.reset")
    assert entries[reset]["turn"] is None
    assert kinds[:reset].count("user.said") == kinds[reset:].count("user.said") == 1
    # Each request still reads back exactly what it was sent, on either side of the reset.
    sent = [
        await setup.conversations.log_sent(gem_id, entry["seq"])
        for entry in entries
        if entry["kind"] == "request.sent"
    ]
    assert [one["exact"] for one in sent] == [True, True]
    assert [one["items"] for one in sent] == [before[:2], setup.llm.requests[1]["input"]]
    await _close(socket, linked)


async def test_a_reset_cuts_the_reply_being_written_and_forgets_it_too() -> None:
    never = asyncio.Event()
    setup = await _chat([_delta("Once upon "), never])
    socket, linked = await _open(setup.service())
    socket.send(setup.gem.id, "a story")
    await socket.until("chat.text")

    await _reset(setup)

    assert socket.chat()[-1] == ("chat.cut", "Once upon…")
    assert await setup.stored() == []
    assert setup.conversations._kept == {}
    await _close(socket, linked)


async def test_a_reset_stops_a_search_running_in_the_background() -> None:
    searches = _Searches(_WEATHER)
    setup = await _chat(_looks(), _said("Noon."), search=searches)
    socket, linked = await _open(setup.service())
    await _cut_while_searching(setup, socket)

    await _reset(setup)
    await _settle()

    # Its placeholder is gone with the rest, so its result is never told.
    assert searches.cancelled == [_WEATHER]
    assert await setup.stored() == []
    assert setup.conversations._kept == {}
    await _close(socket, linked)


async def test_a_gem_in_a_call_is_not_reset() -> None:
    setup = await _chat(_said("Hi."))
    socket, linked = await _open(setup.service())
    socket.send(setup.gem.id, "hello")
    await socket.until("chat.done")
    kept = setup.conversations.gem(gem_key(str(setup.gem.id)))
    kept.call = cast("Any", object())

    with pytest.raises(ConflictError, match="the gem is in a call"):
        await _reset(setup)

    kept.call = None
    assert await setup.shown() == [("user", "hello"), ("gem", "Hi.")]
    await _close(socket, linked)


async def test_a_reset_that_cannot_be_stored_fails_and_forgets_nothing() -> None:
    searches = _Searches(_WEATHER)
    setup = await _chat(_looks(), _said("Noon."), search=searches)
    socket, linked = await _open(setup.service())
    await _cut_while_searching(setup, socket)
    shown = await setup.shown()
    setup.docdb.breaks_after = 0

    with pytest.raises(ConnectionError):
        await _reset(setup)
    await _settle()

    setup.docdb.breaks_after = None
    assert await setup.shown() == shown
    # The search in the background carries on: its placeholder is still remembered.
    assert searches.cancelled == []
    assert setup.conversations.gem(gem_key(str(setup.gem.id))).late.pending
    await _close(socket, linked)
    searches.gates[_WEATHER].set()


@pytest.mark.parametrize("gem_id", [str(ObjectId()), "not-an-id"])
async def test_resetting_a_gem_that_is_not_there_is_a_404(gem_id: str) -> None:
    setup = await _chat()

    with pytest.raises(NotFoundError):
        await _reset(setup, gem_id)
