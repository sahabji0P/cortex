"""
Chat entity: written chat with the gems, one reply at a time for each gem.

The database is the truth, not the connection. For every message:

1. the gem's config and its written conversation so far are loaded from the database,
2. the instructions are built from the config as it is now,
3. one reply is made by the shared loop (see `written_ent.py`), delivered as it is
   written,
4. what the user wrote and what was delivered of the reply are added to the database.

Nothing about a gem's conversation is kept in memory between messages, except the reply
in progress, so that a new message can cut it. So a changed gem applies from the next
message, two windows on one gem cannot drift apart, and a dropped connection loses
nothing.

A call is not joined to this yet: a call starts empty and adds nothing here.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
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
from app.entities.gem_ent import GemEntityClass
from app.entities.instructions import instructions
from app.entities.model import LLM, is_openai, model_for
from app.entities.tools import Toolbox, web_search_tool
from app.entities.written_ent import WrittenConversation, WrittenEvent

__all__ = ["ChatEntityClass", "ChatEvent", "ChatMessage", "ChatTurn", "Deliver"]


class ChatEvent(BaseModel):
    """One frame about a gem's reply, for the client that sent the message.

    - `chat.text`   the next piece of the reply, as it is written
    - `chat.tool`   the gem is using a tool. `text` says what, such as what a search
                    looks up
    - `chat.done`   the reply is complete. `text` is the whole reply
    - `chat.cut`    the reply was cut off by a new message. `text` is what is kept of
                    it, ending in "…"
    - `chat.error`  no reply can be made, or the reply failed. `text` is the reason

    `gem` is the gem's id as the client sent it.
    """

    type: Literal["chat.text", "chat.tool", "chat.done", "chat.cut", "chat.error"]
    gem: str
    text: str


# Sends one frame to the client that sent the message. It raises if the client is gone.
type Deliver = Callable[[ChatEvent], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One message of a gem's written conversation, as it is shown."""

    role: Literal["user", "gem", "tool"]
    text: str
    at: datetime


class ChatTurn:
    """One message to a gem and its reply, from when it is sent until it is stored."""

    def __init__(self) -> None:
        # Set once the reply is stored and its last frame sent (or could not be).
        self.over = asyncio.Event()
        self._cut = False
        self._conversation: WrittenConversation | None = None
        # The task answering it. Kept so it is not lost while it runs.
        self.task: asyncio.Task[None] | None = None

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


class ChatEntityClass:
    def __init__(
        self,
        *,
        gems: GemEntityClass,
        store: ChatStoreClass,
        openai: OpenAIAdapterClass,
        anthropic: AnthropicAdapterClass,
        search: ParallelAdapterClass,
    ) -> None:
        self._gems = gems
        self._store = store
        self._openai = openai
        self._anthropic = anthropic
        self._search = search
        # The latest message to each gem, by the gem's id, until its reply is stored.
        self._turns: dict[str, ChatTurn] = {}

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
        than that are each cut in turn, in the order they came: each is stored, kept
        as just "…", without asking the model.
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
        async def tell(type_: Literal["chat.done", "chat.cut", "chat.error"], said: str) -> None:
            # The last frame of a reply. A client that has gone is not told.
            with contextlib.suppress(Exception):
                await deliver(ChatEvent(type=type_, gem=gem_id, text=said))

        try:
            # The reply before this one is stored first, so this one is made from it.
            if previous is not None:
                await previous.over.wait()
            try:
                gem = await self._gems.get(gem_id)
            except NotFoundError:
                await tell("chat.error", "unknown gem")
                return
            except ValidationError:
                # As when a newer cortex wrote it.
                await tell("chat.error", "the gem's config cannot be read")
                return
            name = gem.model or settings.voice_model
            llm: LLM = self._openai if is_openai(name) else self._anthropic
            if not llm.configured:
                provider = "OpenAI" if is_openai(name) else "Anthropic"
                await tell("chat.error", f"no {provider} key is set, so {name} cannot be used")
                return
            # Writing offers web search when the gem's tools name it and the key is set.
            # Nothing outlives its reply in writing, so a call's cancel_tool_call is not.
            offers_search = self._search.configured and "web_search" in gem.tools
            tools = Toolbox([web_search_tool(self._search)] if offers_search else [])
            model = model_for(llm, name, tools.specs)
            stored = await self._store.load(gem.id)
            conversation = WrittenConversation(
                model=model,
                tools=tools,
                instructions=instructions(
                    gem=gem,
                    search=offers_search,
                    written=True,
                    model_needs=model.own_instructions,
                ),
                # A FIRST RULE, TO BE TRIED OUT, NOT A SETTLED ONE: the whole stored
                # conversation is sent to the model every time. Nothing is trimmed or
                # summed up, and old search results are not cleared, so the context
                # only grows, and quickly when searches are used.
                history=[item.item for item in stored],
            )
            turn.begin(conversation)
            ended, failure = await _read(conversation, text, at, gem_id, deliver)
            try:
                await self._store.add(
                    [
                        ChatItem(
                            gem_id=gem.id,
                            place=len(stored) + offset,
                            at=added.at,
                            item=added.item,
                            shown=added.shown,
                        )
                        for offset, added in enumerate(conversation.added)
                    ]
                )
            except Exception as exc:
                await tell("chat.error", f"the reply could not be stored: {exc}")
                return
            if failure is not None:
                await tell("chat.error", f"reply failed: {failure}")
            elif ended is not None:
                await tell("chat.done" if ended.type == "done" else "chat.cut", ended.text)
        except Exception as exc:
            # Anything else, such as the store being out of reach before the reply began.
            await tell("chat.error", f"no reply could be made: {exc}")
        finally:
            turn.over.set()
            if self._turns.get(key) is turn:
                del self._turns[key]


async def _read(
    conversation: WrittenConversation, text: str, at: datetime, gem_id: str, deliver: Deliver
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


def _key(gem_id: str) -> str:
    """The same gem by whichever spelling of its id. One that is not an id is kept as is."""
    return str(ObjectId(gem_id)) if ObjectId.is_valid(gem_id) else gem_id
