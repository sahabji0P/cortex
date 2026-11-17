"""
Chat entity: written chat with the gems, one reply at a time for each gem.

The database is the truth, not the connection. For every message:

1. the gem's config, the Darbar text and the gem's written conversation so far are
   loaded from the database,
2. the instructions are built from the config and the Darbar text as they are now,
3. one reply is made by the shared loop (see `written_ent.py`), delivered as it is
   written,
4. what the user wrote and what was delivered of the reply are added to the database.

Little about a gem's conversation is kept in memory between messages: the reply in
progress, so that a new message can cut it, and the tool calls still running, so that
their results can be told. So a changed gem applies from the next message, two windows
on one gem cannot drift apart, and a dropped connection loses nothing.

A tool call outlives a reply that is cut, as in a call, and by the same code (see
`late_results.py`). Its call is stored with the placeholder as its result, and the new
message is answered first. When the call finishes, at the next moment no reply is being
written to that gem, its result is added to the stored conversation as a stub call
followed by the result, and the gem tells the user about it in a reply of its own, sent
to the socket that sent the latest message to it. Which stub delivered which call is read
from the stub's arguments, and which results are placeholders and which cancels stopped
a call, from the marks stored with them.

Running calls live in this process's memory, so a restart loses them. A placeholder whose
call is gone is closed with a stub saying so, before the next reply is made.

A call is not joined to this yet: a call starts empty and adds nothing here.
"""

import asyncio
import contextlib
import functools
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from bson import ObjectId
from pydantic import BaseModel, ValidationError

from app.adapters.anthropic_adp import AnthropicAdapterClass
from app.adapters.openai_adp import OpenAIAdapterClass
from app.adapters.parallel_adp import ParallelAdapterClass
from app.config import settings
from app.core.errors import NotFoundError
from app.entities.chat_store import ChatItem, ChatStoreClass
from app.entities.darbar_ent import DarbarEntityClass
from app.entities.gem_ent import GemConfig, GemEntityClass
from app.entities.instructions import instructions
from app.entities.late_results import LateResult, LateResults, delivers, late_stub
from app.entities.loop import ToolCall
from app.entities.model import LLM, Item, ModelName, is_openai, model_for
from app.entities.tools import Tool, Toolbox, web_search_tool
from app.entities.written_ent import WrittenConversation, WrittenEvent, shown

__all__ = ["ChatEntityClass", "ChatEvent", "ChatMessage", "ChatTurn", "Deliver"]


class ChatEvent(BaseModel):
    """One frame about a gem's reply, for the client that sent the message.

    - `chat.result` a search the gem started in an earlier reply has come back, and a
                    reply about it follows. `text` is what the search showed (its query)
    - `chat.text`   the next piece of the reply, as it is written
    - `chat.tool`   the gem is using a tool. `text` says what, such as what a search
                    looks up
    - `chat.done`   the reply is complete. `text` is the whole reply
    - `chat.cut`    the reply was cut off by a new message. `text` is what is kept of
                    it (just "…" if nothing is)
    - `chat.error`  no reply can be made, or the reply failed. `text` is the reason

    `gem` is the gem's id as the client sent it.
    """

    type: Literal["chat.result", "chat.text", "chat.tool", "chat.done", "chat.cut", "chat.error"]
    gem: str
    text: str


# Sends one frame to the client that sent the message. It raises if the client is gone.
type Deliver = Callable[[ChatEvent], Awaitable[None]]

# The result a call lost to a restart is given, so its placeholder does not promise a
# result for ever. THE OWNER HAS NOT RULED ON THIS WORDING.
_LOST = (
    "This call was lost before it finished, and its result will not arrive. Make it again "
    "if it is still needed."
)


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One message of a gem's written conversation, as it is shown."""

    role: Literal["user", "gem", "tool"]
    text: str
    at: datetime


class ChatTurn:
    """One reply of a gem's, from when it is asked for until it is stored.

    Most answer a message. One with no message tells the user of late search results.
    """

    def __init__(self) -> None:
        # Set once the reply is stored and its last frame sent (or could not be).
        self.over = asyncio.Event()
        self._cut = False
        self._conversation: WrittenConversation | None = None
        # The task answering it. Kept so it is not lost while it runs.
        self.task: asyncio.Task[None] | None = None
        # For a reply with no message: the client it is being written for. A reply to a
        # message is cut by whoever sent it. This one nobody asked for, so it is cut
        # when that client is known to have gone (see `ChatEntityClass.left`).
        self.unasked_for: Deliver | None = None

    @property
    def was_cut(self) -> bool:
        return self._cut

    def cut(self) -> None:
        """Cut the reply off where it is. One that has not begun yet is cut as it begins."""
        self._cut = True
        if self._conversation is not None:
            self._conversation.cut()

    def begin(self, conversation: WrittenConversation) -> None:
        """The reply is about to be written in `conversation`."""
        self._conversation = conversation
        if self._cut:
            conversation.cut()


@dataclass(slots=True)
class _Outliving:
    """What outlives a gem's replies: its calls still running, and who to tell."""

    # The gem's calls that outlived their replies, until their results are told.
    late: LateResults
    # The latest message to the gem: its id as the client spelled it, and its client.
    latest: tuple[str, Deliver] | None = None


@dataclass(frozen=True, slots=True)
class _Ready:
    """What a reply of a gem's is made with."""

    gem: GemConfig
    llm: LLM
    name: ModelName


class ChatEntityClass:
    def __init__(
        self,
        *,
        gems: GemEntityClass,
        darbar: DarbarEntityClass,
        store: ChatStoreClass,
        openai: OpenAIAdapterClass,
        anthropic: AnthropicAdapterClass,
        search: ParallelAdapterClass,
    ) -> None:
        self._gems = gems
        self._darbar = darbar
        self._store = store
        self._openai = openai
        self._anthropic = anthropic
        self._search = search
        # The reply being written to each gem, by the gem's id, until it is stored: the
        # one-reply-at-a-time slot. A gem with none here is quiet.
        self._turns: dict[str, ChatTurn] = {}
        # What outlives each gem's replies, by the gem's id.
        self._outliving: dict[str, _Outliving] = {}

    async def messages(self, gem_id: str) -> list[ChatMessage]:
        """A gem's written conversation as it is shown, oldest first.

        Raises `NotFoundError` if there is no such gem. A gem whose config cannot be
        read still has its conversation shown.
        """
        if not await self._gems.exists(gem_id):
            raise NotFoundError("Gem")
        shown: list[ChatMessage] = []
        for stored in await self._store.load(ObjectId(gem_id)):
            item = stored.item
            if "role" in item:
                if item["role"] == "user":
                    shown.append(ChatMessage("user", item["content"], stored.at))
                elif item["role"] == "assistant":
                    shown.append(ChatMessage("gem", item["content"], stored.at))
            # Stubs and cancels are stored with nothing to show.
            elif item["type"] == "function_call" and stored.shown is not None:
                shown.append(ChatMessage("tool", stored.shown, stored.at))
        return shown

    def send(self, gem_id: str, text: str, deliver: Deliver) -> ChatTurn:
        """Send a message to a gem. Its reply's frames go to `deliver`, as it is written.

        Returns at once. The reply ends with exactly one of `chat.done`, `chat.cut` or
        `chat.error`, and is stored before that frame is sent. `ChatTurn.over` is set
        once it is.

        A FIRST RULE, TO BE TRIED OUT, NOT A SETTLED ONE: a new message to a gem whose
        reply is still being written cuts that reply, as speaking over someone does in
        a call. The reply is kept as what was sent, ending in "…", its client gets
        `chat.cut`, and then the new message is answered. Messages that come faster
        than that are each cut in turn, in the order they came: each is stored, with
        no reply, without asking the model.

        A reply that tells of a late search result is cut the same way. Later replies of
        that kind go to the client of the latest message.
        """
        at = datetime.now(UTC)
        key = _key(gem_id)
        turn = ChatTurn()
        previous = self._turns.get(key)
        self._turns[key] = turn
        if previous is not None:
            previous.cut()
        turn.task = asyncio.create_task(
            self._answer(turn, key, previous, gem_id, text, at, deliver)
        )
        return turn

    async def _answer(
        self,
        turn: ChatTurn,
        key: str,
        previous: ChatTurn | None,
        gem_id: str,
        text: str,
        at: datetime,
        deliver: Deliver,
    ) -> None:
        tell = _teller(gem_id, deliver)
        try:
            # The reply before this one is stored first, so this one is made from it.
            if previous is not None:
                await previous.over.wait()
            ready = await self._ready(gem_id)
            if isinstance(ready, str):
                await tell("chat.error", ready)
                return
            # Noted only for a gem that is there, so an id that names none keeps nothing.
            # Messages are answered in the order they came, so the last to note is the
            # latest. One that was cut is not noted: a newer message notes its own
            # client, and a client that has gone (which cuts its replies) is not to be
            # written to again.
            if not turn.was_cut:
                self._state(key).latest = (gem_id, deliver)
            stored = await self._store.load(ready.gem.id)
            history = [item.item for item in stored]
            # A call lost to a restart is closed before the new message.
            history += await self._add_stubs(ready.gem.id, history, self._lost(key, stored))
            await self._write(turn, key, ready, history, text, at, gem_id, deliver)
        except Exception as exc:
            # Anything else, such as the store being out of reach before the reply began.
            await tell("chat.error", f"no reply could be made: {exc}")
        finally:
            self._release(turn, key)

    async def _ready(self, gem_id: str) -> _Ready | str:
        """The gem and the model to answer with, or why no reply can be made."""
        try:
            gem = await self._gems.get(gem_id)
        except NotFoundError:
            return "unknown gem"
        except ValidationError:
            # As when a newer cortex wrote it.
            return "the gem's config cannot be read"
        name = gem.model or settings.voice_model
        llm: LLM = self._openai if is_openai(name) else self._anthropic
        if not llm.configured:
            provider = "OpenAI" if is_openai(name) else "Anthropic"
            return f"no {provider} key is set, so {name} cannot be used"
        return _Ready(gem, llm, name)

    async def _write(
        self,
        turn: ChatTurn,
        key: str,
        ready: _Ready,
        history: list[Item],
        text: str | None,
        at: datetime,
        gem_id: str,
        deliver: Deliver,
    ) -> None:
        """Write one reply from `history`, deliver it, store it, and send its last frame.

        `text` is the user's new message, or None for a reply with no message.
        """
        tell = _teller(gem_id, deliver)
        gem = ready.gem
        late = self._state(key).late
        # Writing offers web search when the gem's tools name it and the key is set, and
        # with it, as in a call, cancel_tool_call to stop a call that carries on.
        offers_search = self._search.configured and "web_search" in gem.tools
        search = web_search_tool(self._search)
        cancel = late.tool
        if offers_search:
            tools = Toolbox([search, cancel])
        else:
            # The conversation holds calls from when the gem could search. A provider
            # refuses a history with calls of a tool it is not told of, so such a tool
            # stays declared, and a call of it now is answered with why not.
            tools = Toolbox(
                [
                    Tool(tool.spec, _not_available)
                    for tool in (search, cancel)
                    if any(_calls(item, tool.name) for item in history)
                ]
            )
        model = model_for(ready.llm, ready.name, tools.specs)
        # Read for every reply, so a change to it applies from the next message.
        darbar = (await self._darbar.read()).text
        conversation = WrittenConversation(
            model=model,
            tools=tools,
            instructions=instructions(
                darbar=darbar,
                gem=gem,
                search=offers_search,
                written=True,
                model_needs=model.own_instructions,
            ),
            # A FIRST RULE, TO BE TRIED OUT, NOT A SETTLED ONE: the whole stored
            # conversation is sent to the model every time. Nothing is trimmed or
            # summed up, and old search results are not cleared, so the context
            # only grows, and quickly when searches are used.
            history=history,
            late=late,
        )
        turn.begin(conversation)
        ended, failure = await _read(conversation, text, at, gem_id, deliver)
        try:
            await self._store.add(
                [
                    ChatItem(
                        gem_id=gem.id,
                        place=len(history) + offset,
                        at=added.at,
                        item=added.item,
                        shown=added.shown,
                        mark=added.mark,
                    )
                    for offset, added in enumerate(conversation.added)
                ]
            )
        except Exception as exc:
            # The calls left running are in no stored conversation: nothing could take
            # their results.
            late.forget(conversation.left_running)
            await tell("chat.error", f"the reply could not be stored: {exc}")
            return
        # From here on their placeholders are stored, so their results have to be taken
        # when they come, whatever fails next.
        if not await self._gems.exists(gem_id):
            # The gem was deleted while its reply was being written. Its conversation
            # went with it, so what was just added goes too, and so do its calls.
            await self._store.forget(gem.id)
            late.forget(conversation.left_running)
        if failure is not None:
            await tell("chat.error", f"reply failed: {failure}")
        elif ended is not None:
            await tell("chat.done" if ended.type == "done" else "chat.cut", ended.text)

    def _release(self, turn: ChatTurn, key: str) -> None:
        """A reply is stored and its last frame sent: free the gem's slot."""
        turn.over.set()
        if self._turns.get(key) is turn:
            del self._turns[key]
        # Results that came in while it was written are told now.
        self._tell_when_quiet(key)
        self._forget_if_idle(key)

    def left(self, deliver: Deliver) -> None:
        """A client has gone: `deliver` is the function its frames were sent with.

        Nothing more is written for it. A reply nobody asked for that is being written
        for it is cut, as its replies to messages are cut by whoever holds them, and
        later results for the gems it wrote to are only stored until someone writes again.
        """
        for key, state in list(self._outliving.items()):
            if state.latest is not None and state.latest[1] is deliver:
                state.latest = None
                self._forget_if_idle(key)
        for turn in self._turns.values():
            if turn.unasked_for is deliver:
                turn.cut()

    def _state(self, key: str) -> _Outliving:
        state = self._outliving.get(key)
        if state is None:
            late = LateResults(functools.partial(self._tell_when_quiet, key))
            state = self._outliving[key] = _Outliving(late)
        return state

    def _forget_if_idle(self, key: str) -> None:
        """Keep nothing for a gem with no reply being written, no call running, no result
        waiting and no client. A reply being written may still leave a call running."""
        state = self._outliving.get(key)
        if state is None or key in self._turns:
            return
        if not state.late.pending and state.latest is None:
            del self._outliving[key]

    def _tell_when_quiet(self, key: str) -> None:
        """If results are waiting and no reply is being written to the gem, tell them.

        Whether the gem is quiet and the taking of its slot are done with no `await`
        between them, so a message that arrives at that instant waits for this reply.
        """
        state = self._outliving.get(key)
        if state is None or not state.late.ready or key in self._turns:
            return
        turn = ChatTurn()
        self._turns[key] = turn
        turn.task = asyncio.create_task(self._tell(turn, key))

    async def _tell(self, turn: ChatTurn, key: str) -> None:
        """Add the waiting results to the stored conversation, then have the gem tell them.

        The reply has no new message. It goes to the client of the latest message to the
        gem, if that client is still there. If it is not, the results are only stored:
        the gem's instructions tell it to bring them up when it next answers.
        """
        state = self._state(key)
        # The client the reply is for, once `chat.result` has reached it: it is then owed
        # a last frame. `gem_id` is the gem's id as that client spelled it.
        owed: Deliver | None = None
        gem_id = key
        try:
            if turn.was_cut:
                return  # a message came first: the results wait until it is answered
            # Taken at once, so that whatever happens next they are not taken again.
            told = state.late.take()
            if not await self._gems.exists(key):
                return  # the gem went, and its conversation with it: they are dropped
            gem_oid = ObjectId(key)
            stored = await self._store.load(gem_oid)
            history = [item.item for item in stored]
            lost = self._lost(key, stored, told=told)
            stubs = [*lost, *(late.stub for late in told)]
            history += await self._add_stubs(gem_oid, history, stubs)
            if not await self._gems.exists(key):
                await self._store.forget(gem_oid)  # deleted while they were stored
                return
            if state.latest is None:
                return  # nobody is there to read a reply: the results are only stored
            gem_id, deliver = state.latest
            # Noted with no `await` since the client was read, so that if it goes from
            # here on, `left` finds this reply and cuts it.
            turn.unasked_for = deliver
            ready = await self._ready(key)
            if isinstance(ready, str) or turn.was_cut:
                # No reply can be made, or a message came first (or the client went): the
                # results are stored, and the gem's instructions tell it to bring them up
                # when it answers.
                return
            try:
                for late in told:
                    text = shown(late.call) or ""
                    await deliver(ChatEvent(type="chat.result", gem=gem_id, text=text))
            except Exception:
                return  # the client has gone: the results stay stored, untold
            owed = deliver
            await self._write(turn, key, ready, history, None, datetime.now(UTC), gem_id, deliver)
        except Exception as exc:
            # As when the store is out of reach. A result taken and not stored is lost:
            # its placeholder is closed as lost before the next reply. A client sent
            # `chat.result` is owed a last frame: `_write` sends one only once nothing
            # after it can fail.
            if owed is not None:
                await _teller(gem_id, owed)("chat.error", f"no reply could be made: {exc}")
        finally:
            self._release(turn, key)

    async def _add_stubs(
        self, gem_id: ObjectId, history: Sequence[Item], stubs: Sequence[ToolCall]
    ) -> list[Item]:
        """Store each stub call and its result after the conversation. Return the items."""
        items = [item for stub in stubs for item in stub.items()]
        now = datetime.now(UTC)
        await self._store.add(
            [
                ChatItem(gem_id=gem_id, place=len(history) + offset, at=now, item=item)
                for offset, item in enumerate(items)
            ]
        )
        return items

    def _lost(
        self, key: str, stored: Sequence[ChatItem], *, told: Sequence[LateResult] = ()
    ) -> list[ToolCall]:
        """A stub for each call lost to a restart, closing its placeholder.

        Lost is a placeholder whose call is not running or waiting here (or being `told`
        now), that no stub has delivered, and that no `cancel_tool_call` stopped. Each is
        known by the mark stored with its result, never by the result's wording.
        """
        state = self._outliving.get(key)
        here = {late.call.call_id for late in told}
        items = [entry.item for entry in stored]
        calls = {
            item["call_id"]: item
            for item in items
            if "type" in item and item["type"] == "function_call"
        }
        delivered = {original for item in items if (original := delivers(item))}
        placeholders: list[str] = []
        cancelled: set[str | None] = set()
        for entry in stored:
            item = entry.item
            if "type" not in item or item["type"] != "function_call_output":
                continue
            if entry.mark == "placeholder":
                placeholders.append(item["call_id"])
            elif entry.mark == "cancelled" and item["call_id"] in calls:
                cancelled.add(_named(calls[item["call_id"]]["arguments"]))
        lost: list[ToolCall] = []
        for call_id in placeholders:
            if call_id not in calls or call_id in here or call_id in delivered:
                continue
            if call_id in cancelled or (state is not None and state.late.holds(call_id)):
                continue
            call = calls[call_id]
            lost.append(late_stub(ToolCall(call_id, call["name"], call["arguments"]), _LOST))
        return lost


def _teller(
    gem_id: str, deliver: Deliver
) -> Callable[[Literal["chat.done", "chat.cut", "chat.error"], str], Awaitable[None]]:
    async def tell(type_: Literal["chat.done", "chat.cut", "chat.error"], said: str) -> None:
        # The last frame of a reply. A client that has gone is not told.
        with contextlib.suppress(Exception):
            await deliver(ChatEvent(type=type_, gem=gem_id, text=said))

    return tell


def _named(arguments: str) -> str | None:
    """The call_id a `cancel_tool_call` named, if it can be read."""
    try:
        named = json.loads(arguments)["call_id"]
    except (ValueError, KeyError, TypeError):
        return None
    return str(named)


async def _read(
    conversation: WrittenConversation,
    text: str | None,
    at: datetime,
    gem_id: str,
    deliver: Deliver,
) -> tuple[WrittenEvent | None, Exception | None]:
    """Deliver one reply as it is written. Return the event that ended it, or its failure.

    A client that has gone ends the reading, which cuts the reply where it was, and
    then nothing ended it that can be told.
    """
    try:
        async with contextlib.aclosing(conversation.send(text, at=at)) as stream:
            async for event in stream:
                if event.type in ("done", "cut"):
                    return event, None
                type_ = "chat.text" if event.type == "text" else "chat.tool"
                try:
                    await deliver(ChatEvent(type=type_, gem=gem_id, text=event.text))
                except Exception:
                    return None, None
    except Exception as exc:
        return None, exc
    return None, None


def _calls(item: Item, tool: str) -> bool:
    """Whether a history item is a call of the tool called `tool`."""
    return "type" in item and item["type"] == "function_call" and item["name"] == tool


async def _not_available(arguments: str) -> str:
    return "This tool is not available to you now."


def _key(gem_id: str) -> str:
    """The same gem by whichever spelling of its id. One that is not an id is kept as is."""
    return str(ObjectId(gem_id)) if ObjectId.is_valid(gem_id) else gem_id
