"""Written chat with a gem: what it is told, what is stored, and the standing socket.

The expected instructions are written out here in full, not built from the module's
constants, so any change to what the model reads shows up as a failing test.
"""

import asyncio
import json
from collections.abc import Iterable, MutableMapping, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal, cast

import pytest
from bson import ObjectId
from pydantic import BaseModel

from app.core.errors import NotFoundError
from app.entities.chat_ent import ChatEntityClass
from app.entities.chat_store import ChatItem, ChatStoreClass
from app.entities.gem_ent import GemConfig, GemEntityClass
from app.entities.instructions import instruction_layers, instructions
from app.routers.gems.service import GemsServiceV1Class
from app.routers.link.service import LinkServiceV1Class
from tests.test_voice_conversation import _completed, _delta, _Search, _search_call, _starts

if TYPE_CHECKING:
    from fastapi import WebSocket

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


# ---- chat over the standing socket, with the model and the search faked


class _LLM:
    """The OpenAI adapter, scripted: each request plays the next round of stream events.

    A round's `asyncio.Event` pauses the stream there until the test sets it. Every
    request starts with the whole of the one before it for the same conversation (told
    apart by its first message), which is the rule the history keeps.
    """

    configured = True

    def __init__(self, *rounds: list[Any]) -> None:
        self._rounds = list(rounds)
        self.requests: list[dict[str, Any]] = []

    def then(self, *rounds: list[Any]) -> None:
        self._rounds.extend(rounds)

    async def stream_response(self, **request: Any) -> Any:
        sent = [dict(item) for item in request["input"]]
        same = [r for r in self.requests if r["input"][:1] == sent[:1]]
        if same:
            before = same[-1]["input"]
            assert sent[: len(before)] == before, "an earlier item was changed"
        self.requests.append({**request, "input": sent})
        script = self._rounds.pop(0)

        async def stream() -> Any:
            for step in script:
                if isinstance(step, asyncio.Event):
                    await step.wait()
                else:
                    yield step

        return stream()


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
    store: ChatStoreClass
    llm: _LLM
    search: Any
    chat: ChatEntityClass
    gem: GemConfig

    def service(self) -> LinkServiceV1Class:
        return LinkServiceV1Class(self.chat)

    def again(self) -> "_Chat":
        """The same database, read by a new chat entity, as after a restart."""
        chat = ChatEntityClass(
            gems=self.gems,
            store=self.store,
            openai=cast("Any", self.llm),
            anthropic=cast("Any", _NoKey()),
            search=self.search,
        )
        return _Chat(**{**vars(self), "chat": chat})

    async def stored(self) -> list[Any]:
        return [item.item for item in await self.store.load(self.gem.id)]

    async def shown(self) -> list[tuple[str, str]]:
        chat = await GemsServiceV1Class(self.gems, self.chat).get_chat(str(self.gem.id))
        return [(message.role, message.text) for message in chat.messages]


async def _chat(*rounds: list[Any], search: Any = None, **gem: Any) -> _Chat:
    docdb, adapter = _docdb()
    gems, store = GemEntityClass(adapter), ChatStoreClass(adapter)
    llm = _LLM(*rounds)
    search = _Search() if search is None else search
    chat = ChatEntityClass(
        gems=gems,
        store=store,
        openai=cast("Any", llm),
        anthropic=cast("Any", _NoKey()),
        search=search,
    )
    made = await gems.create(
        **{
            "name": "Kuber",
            "instructions": "You look after the user's personal finances.",
            "model": "6 Luna",
            **gem,
        }
    )
    return _Chat(docdb=docdb, gems=gems, store=store, llm=llm, search=search, chat=chat, gem=made)


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

    assert socket.frames[0] == {"type": "link.ready", "version": 1}
    assert socket.frames[1:] == [
        {"type": "chat.text", "gem": str(setup.gem.id), "text": "Hi "},
        {"type": "chat.text", "gem": str(setup.gem.id), "text": "there."},
        {"type": "chat.done", "gem": str(setup.gem.id), "text": "Hi there."},
    ]
    assert await setup.stored() == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi there."},
    ]
    assert await setup.shown() == [("user", "hello"), ("gem", "Hi there.")]
    [request] = setup.llm.requests
    today = datetime.now(UTC).strftime("%A, %d %B %Y")
    assert request["instructions"] == (
        _KUBER_IN_WRITING + _WRITTEN_SEARCH_RULES + f"\nToday is {today}.\n"
    )
    # Search, but nothing about searches that carry on in the background.
    assert _tool_names(request) == ["web_search"]
    await _close(socket, linked)


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
    assert [item.place for item in stored] == [0, 1, 2, 3]
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
        {"role": "user", "content": "a story"},
        {"role": "assistant", "content": "Once upon a time…"},
        {"role": "user", "content": "actually, the weather"},
        {"role": "assistant", "content": "Sure."},
    ]
    assert setup.llm.requests[1]["input"] == (await setup.stored())[:3]
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
    assert [item["content"] for item in await setup.stored()] == [
        "a story",
        "Once upon…",
        "no wait",
        "…",
        "the weather",
        "Sure.",
    ]
    await _close(socket, linked)


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
    result = setup.llm.requests[1]["input"][3]
    assert result["type"] == "function_call_output"
    assert "Norris won in Las Vegas." in result["output"]
    stored = await setup.stored()
    assert stored[:5] == [
        *setup.llm.requests[1]["input"][:4],
        {"role": "assistant", "content": "Sunny."},
    ]
    assert setup.llm.requests[2]["input"] == [*stored[:5], {"role": "user", "content": "thanks"}]
    assert await setup.shown() == [
        ("user", "weather?"),
        ("gem", "Let me look."),
        ("tool", "Pune weather"),
        ("gem", "Sunny."),
        ("user", "thanks"),
        ("gem", "Bye."),
    ]
    await _close(socket, linked)


async def test_a_tool_still_running_at_a_cut_is_stopped_and_stored_with_a_result() -> None:
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
        ("chat.cut", "Let me look. …"),
        ("chat.text", "Okay."),
        ("chat.done", "Okay."),
    ]
    stored = await setup.stored()
    assert [item.get("role") or item["type"] for item in stored] == [
        "user",
        "assistant",
        "function_call",
        "function_call_output",
        "assistant",
        "user",
        "assistant",
    ]
    assert stored[3]["output"].startswith("Stopped:")
    assert stored[4] == {"role": "assistant", "content": "…"}
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
    assert setup.docdb.collections.get("ChatItems", []) == []
    assert setup.llm.requests == []
    await _close(socket, linked)


async def test_a_reply_that_fails_part_way_keeps_what_was_sent() -> None:
    setup = await _chat([_delta("Half "), SimpleNamespace(type="error", message="boom")])
    socket, linked = await _open(setup.service())

    socket.send(setup.gem.id, "go")
    await socket.until("chat.error")

    assert socket.chat() == [("chat.text", "Half "), ("chat.error", "reply failed: boom")]
    assert await setup.stored() == [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "Half…"},
    ]
    await _close(socket, linked)


async def test_a_reply_that_cannot_be_stored_ends_in_chat_error() -> None:
    setup = await _chat(_said("Hi."), _said("Hi again."))
    # Another process took the conversation's next place first.
    await setup.store.add([_item(setup.gem.id, 0, "from elsewhere")])
    setup.chat = ChatEntityClass(
        gems=setup.gems,
        store=cast("Any", _BlindStore(setup.store)),
        openai=cast("Any", setup.llm),
        anthropic=cast("Any", _NoKey()),
        search=setup.search,
    )
    socket, linked = await _open(setup.service())

    socket.send(setup.gem.id, "hello")
    await socket.until("chat.error")

    assert socket.chat()[-1] == (
        "chat.error",
        "the reply could not be stored: duplicate key: that place is taken",
    )
    await _close(socket, linked)


class _BlindStore:
    """A store that does not see what another process wrote."""

    def __init__(self, store: ChatStoreClass) -> None:
        self._store = store

    async def load(self, gem_id: ObjectId) -> list[ChatItem]:
        return []

    async def add(self, items: Sequence[ChatItem]) -> None:
        await self._store.add(items)


@pytest.mark.parametrize(
    "frame",
    [
        "not json",
        json.dumps({"type": "chat.shout", "gem": "x", "text": "hi"}),
        json.dumps({"type": "chat.send", "gem": "x"}),
        json.dumps({"type": "chat.send", "gem": "x", "text": "   "}),
        json.dumps({"type": "chat.send", "gem": "x", "text": "a" * 20_001}),
        None,  # a binary frame
    ],
)
async def test_a_frame_that_cannot_be_read_gets_error_and_the_socket_stays_open(
    frame: str | None,
) -> None:
    setup = await _chat(_said("Hi."))
    socket, linked = await _open(setup.service())

    if frame is None:
        socket.raw(data=b"\x00\x01")
    else:
        socket.raw(frame)
    await socket.until("error")
    socket.send(setup.gem.id, "hello")
    await socket.until("chat.done")

    assert socket.frames[1]["type"] == "error"
    assert socket.frames[1]["text"]
    assert not linked.done()
    assert setup.llm.requests[0]["input"] == [{"role": "user", "content": "hello"}]
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
    assert [item["content"] for item in await setup.stored()] == [
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
        {"name": "Lakshmi", "instructions": "You keep the household accounts.", "tools": []},
    )
    socket.send(setup.gem.id, "hello again")
    await socket.until("chat.done", 2)

    first, second = setup.llm.requests
    assert (
        "You are Kuber.\n\nYou look after the user's personal finances.\n\n"
        in (first["instructions"])
    )
    assert "You are Lakshmi.\n\nYou keep the household accounts.\n\n" in second["instructions"]
    assert "search" not in second["instructions"]
    assert _tool_names(first) == ["web_search"]
    assert _tool_names(second) == []
    # The conversation carries on as it was stored.
    assert second["input"][:2] == [
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
    assert "search" not in request["instructions"]
    await _close(socket, linked)


# ---- the history route


async def test_the_history_is_shown_oldest_first_with_times_in_utc() -> None:
    setup = await _chat()
    await setup.store.add(
        [
            _item(setup.gem.id, 0, "what is my balance?"),
            ChatItem(
                gem_id=setup.gem.id,
                place=1,
                at=_NOW,
                item={
                    "type": "function_call",
                    "call_id": "c",
                    "name": "web_search",
                    "arguments": "{}",
                },
                shown="current balance of the savings account",
            ),
            ChatItem(
                gem_id=setup.gem.id,
                place=2,
                at=_NOW,
                item={"type": "function_call_output", "call_id": "c", "output": "40,000"},
            ),
            ChatItem(
                gem_id=setup.gem.id,
                place=3,
                at=_NOW,
                item={"role": "assistant", "content": "It is forty thousand rupees."},
            ),
        ]
    )

    chat = await GemsServiceV1Class(setup.gems, setup.chat).get_chat(str(setup.gem.id))

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
        await GemsServiceV1Class(setup.gems, setup.chat).get_chat(gem_id)


async def test_a_deleted_gem_has_no_history() -> None:
    setup = await _chat(_said("Hi."))
    socket, linked = await _open(setup.service())
    socket.send(setup.gem.id, "hello")
    await socket.until("chat.done")
    await _close(socket, linked)

    await setup.gems.delete(str(setup.gem.id))

    assert setup.docdb.collections["ChatItems"] == []
    with pytest.raises(NotFoundError):
        await GemsServiceV1Class(setup.gems, setup.chat).get_chat(str(setup.gem.id))
