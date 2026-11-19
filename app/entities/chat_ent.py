"""
Chat entity: written chat with the gems, one reply at a time for each gem.

The database is the truth, not the connection. For every message:

1. the gem's config, the Darbar text and the gem's conversation so far are loaded from
   the database,
2. the instructions are built from the config and the Darbar text as they are now,
3. one reply is made by the shared loop (see `written_ent.py`), delivered as it is
   written,
4. what the user wrote and what was delivered of the reply are added to the database.

What is kept in memory of a gem's conversation is in `conversation_ent.py`: the reply
in progress, so that a new message can cut it, the tool calls still running, so that
their results can be told, and messages no reply has got through to yet. So a changed
gem applies from the next message, two windows on one gem cannot drift apart, and a
dropped connection loses nothing.

A gem has one conversation, whether the user calls it or writes to it: a reply in
writing is made from everything stored, a call's turns included. While a call holds the
gem, nothing is written to it: a message is answered with `chat.error`. The first
written turn after a call opens with a line that says the call has ended (see
`history.py`).

A tool call outlives a reply that is cut, as in a call, and by the same code (see
`late_results.py`). Its call is stored with the placeholder as its result, and the new
message is answered. When the call finishes, at the next moment no reply is being
written to that gem, its result is added to the stored conversation as a stub call
followed by the result, and the gem tells the user about it in a reply of its own, sent
to the socket that sent the latest message to it. If a message is waiting to be answered
then, the result is added ahead of that message instead, and the reply to the message
tells it, as in a call (see `history.py`). Which stub delivered which call is read
from the stub's arguments, and which results are placeholders and which cancels stopped
a call, from the marks stored with them.
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
from app.entities.conversation_ent import ConversationEntityClass, Stored, Writer, gem_key
from app.entities.darbar_ent import DarbarEntityClass
from app.entities.gem_ent import GemConfig, GemEntityClass
from app.entities.history import UserTurn, mode_line, settled, shown, told
from app.entities.instructions import instructions
from app.entities.late_results import LateResult
from app.entities.model import LLM, ModelName, is_openai, model_for
from app.entities.tools import gem_toolbox
from app.entities.written_ent import WrittenConversation, WrittenEvent

__all__ = ["ChatEntityClass", "ChatEvent", "ChatTurn", "Deliver"]


class ChatEvent(BaseModel):
    """One frame about a gem's reply, for the client that sent the message.

    - `chat.result` a search the gem started in an earlier reply has come back, and a
                    reply about it follows, unasked. `text` is what the search showed
                    (its query). A result told in the reply to a message has none
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
        conversations: ConversationEntityClass,
        openai: OpenAIAdapterClass,
        anthropic: AnthropicAdapterClass,
        search: ParallelAdapterClass,
    ) -> None:
        self._gems = gems
        self._darbar = darbar
        self._conversations = conversations
        self._openai = openai
        self._anthropic = anthropic
        self._search = search
        # The latest message to each gem, by `gem_key`: its id as the client spelled it,
        # and its client. A late result is told to that client.
        self._latest: dict[str, tuple[str, Deliver]] = {}
        # A gem's late results are told here, in writing.
        conversations.tell_results_with(self._tell_when_quiet)

    def send(self, gem_id: str, text: str, deliver: Deliver) -> ChatTurn:
        """Send a message to a gem. Its reply's frames go to `deliver`, as it is written.

        Returns at once. The reply ends with exactly one of `chat.done`, `chat.cut` or
        `chat.error`, and is stored before that frame is sent. `ChatTurn.over` is set
        once it is.

        A FIRST RULE, TO BE TRIED OUT, NOT A SETTLED ONE: a new message to a gem whose
        reply is still being written cuts that reply, as speaking over someone does in
        a call. The reply is kept as what was sent, ending in "…", its client gets
        `chat.cut`, and then the new message is answered. Messages that come faster
        than that are each cut in turn, in the order they came, without asking the
        model. A message whose reply left nothing in the history is not stored on its
        own: the next message joins it, as words do in a call (see `history.py`).

        A reply that tells of a late search result is cut the same way. Later replies of
        that kind go to the client of the latest message.

        While a call holds the gem, the message is answered with `chat.error` and kept
        nowhere, and the call is not disturbed.
        """
        at = datetime.now(UTC)
        key = gem_key(gem_id)
        turn = ChatTurn()
        held = self._conversations.find(key)
        if held is not None and held.call is not None:
            turn.task = asyncio.create_task(self._refuse(turn, gem_id, deliver))
            return turn
        kept = self._conversations.gem(key)
        previous, kept.writer = kept.writer, turn
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
        previous: Writer | None,
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
                self._latest[key] = (gem_id, deliver)
            # A call lost to a restart is closed before the new message. Late results
            # waiting go in ahead of it, so the reply to it tells them too. No
            # `chat.result` goes before that reply: the client takes one as the start of
            # a reply nobody asked for. A message cut already leaves them to the next.
            kept = self._conversations.gem(key)
            results = [] if turn.was_cut else kept.late.take()
            history = await self._load(ready.gem.id, kept.turn, results)
            await self._write(turn, key, ready, history, text, at, gem_id, deliver)
        except Exception as exc:
            # Anything else, such as the store being out of reach before the reply began.
            await tell("chat.error", f"no reply could be made: {exc}")
        finally:
            self._release(turn, key)

    async def _refuse(self, turn: ChatTurn, gem_id: str, deliver: Deliver) -> None:
        try:
            await _teller(gem_id, deliver)("chat.error", "the gem is in a call")
        finally:
            turn.over.set()

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
        history: Stored,
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
        kept = self._conversations.gem(key)
        late = kept.late
        tools = gem_toolbox(gem.tools, self._search, late.tool, history.items)
        model = model_for(ready.llm, ready.name, tools.specs)
        # Read for every reply, so a change to it applies from the next message.
        darbar = (await self._darbar.read()).text
        conversation = WrittenConversation(
            model=model,
            tools=tools,
            instructions=instructions(
                darbar=darbar,
                gem=gem,
                model_needs=model.own_instructions,
            ),
            # A FIRST RULE, TO BE TRIED OUT, NOT A SETTLED ONE: the whole stored
            # conversation is sent to the model every time. Nothing is trimmed or
            # summed up, and old search results are not cleared, so the context
            # only grows, and quickly when searches are used.
            history=history.items,
            last_mode=history.mode,
            late=late,
            turn=kept.turn,
        )
        turn.begin(conversation)
        ended, failure = await _read(conversation, text, at, gem_id, deliver)
        try:
            # A line of ours with nothing after it is not stored: the next turn writes it.
            added = conversation.added
            await self._conversations.add(gem.id, history, added[: settled(added)], "chat")
        except Exception as exc:
            # The calls left running are in no stored conversation: nothing could take
            # their results.
            late.forget(conversation.left_running)
            await tell("chat.error", f"the reply could not be stored: {exc}")
            return
        # From here on their placeholders are stored, so their results have to be taken
        # when they come, whatever fails next.
        if await self._conversations.gone(gem.id):
            # The gem was deleted while its reply was being written. Its conversation
            # went with it, so what was just added goes too, and so do its calls.
            late.forget(conversation.left_running)
        if failure is not None:
            await tell("chat.error", f"reply failed: {failure}")
        elif ended is not None:
            await tell("chat.done" if ended.type == "done" else "chat.cut", ended.text)

    def _release(self, turn: ChatTurn, key: str) -> None:
        """A reply is stored and its last frame sent: free the gem's slot."""
        turn.over.set()
        kept = self._conversations.gem(key)
        if kept.writer is turn:
            kept.writer = None
        # Results that came in while it was written are told now.
        self._tell_when_quiet(key)
        self._conversations.let_go(key)

    def left(self, deliver: Deliver) -> None:
        """A client has gone: `deliver` is the function its frames were sent with.

        Nothing more is written for it. A reply nobody asked for that is being written
        for it is cut, as its replies to messages are cut by whoever holds them, and
        later results for the gems it wrote to are only stored until someone writes again.
        """
        for key, (_, client) in list(self._latest.items()):
            if client is deliver:
                del self._latest[key]
        for kept in self._conversations.kept():
            turn = kept.writer
            if isinstance(turn, ChatTurn) and turn.unasked_for is deliver:
                turn.cut()

    def _tell_when_quiet(self, key: str) -> None:
        """If results are waiting and no reply is being written to the gem, tell them.

        Whether the gem is quiet and the taking of its slot are done with no `await`
        between them, so a message that arrives at that instant waits for this reply.
        """
        kept = self._conversations.find(key)
        if kept is None or not kept.late.ready:
            return
        if kept.writer is not None or kept.call is not None:
            return
        turn = ChatTurn()
        kept.writer = turn
        turn.task = asyncio.create_task(self._tell(turn, key))

    async def _tell(self, turn: ChatTurn, key: str) -> None:
        """Add the waiting results to the stored conversation, then have the gem tell them.

        The reply has no new message. It goes to the client of the latest message to the
        gem, if that client is still there. If it is not, the results are only stored:
        the gem's instructions tell it to bring them up when it next answers.
        """
        kept = self._conversations.gem(key)
        # The client the reply is for, once `chat.result` has reached it: it is then owed
        # a last frame. `gem_id` is the gem's id as that client spelled it.
        owed: Deliver | None = None
        gem_id = key
        try:
            if turn.was_cut:
                return  # a message came first: the results wait until it is answered
            # Taken at once, so that whatever happens next they are not taken again.
            results = kept.late.take()
            if not await self._gems.exists(key):
                return  # the gem went, and its conversation with it: they are dropped
            gem_oid = ObjectId(key)
            history = await self._load(gem_oid, kept.turn, results)
            if await self._conversations.gone(gem_oid):
                return  # deleted while they were stored
            latest = self._latest.get(key)
            if latest is None:
                return  # nobody is there to read a reply: the results are only stored
            gem_id, deliver = latest
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
                for late in results:
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

    async def _load(self, gem_id: ObjectId, turn: UserTurn, results: list[LateResult]) -> Stored:
        """The gem's conversation as stored, with the late results taken added after it.

        They are added in writing: after a call, a line says so first. A turn left open
        has been sent to the model: it is closed, and the results follow it.
        """
        history = await self._conversations.load(gem_id, told=results)
        added = told(turn, results)
        if added:
            added = [*mode_line("chat", history.mode), *added]
        return await self._conversations.add(gem_id, history, added, "chat")


def _teller(
    gem_id: str, deliver: Deliver
) -> Callable[[Literal["chat.done", "chat.cut", "chat.error"], str], Awaitable[None]]:
    async def tell(type_: Literal["chat.done", "chat.cut", "chat.error"], said: str) -> None:
        # The last frame of a reply. A client that has gone is not told.
        with contextlib.suppress(Exception):
            await deliver(ChatEvent(type=type_, gem=gem_id, text=said))

    return tell


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
