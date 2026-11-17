"""
Opening a call, and keeping what it adds to the gem's conversation.

A call is always with a gem, and may say who speaks first. Opening it reads the gem's
config and the Darbar text, connects speech recognition, then takes hold of the gem: a
gem has one conversation and one writer at a time (see `conversation_ent.py`). A reply
being written to the gem is cut, as a new message cuts it, and the call opens once that
reply is stored. The call then starts from the gem's conversation as it is stored, and
adds its turns to it. The standing socket (`app/routers/link`) holds the call from there.

What the call adds is stored as it is settled, in order, after what was stored before:
the user's turns, the gem's replies as they were heard, tool calls and their results,
and our own lines. A line of ours is stored with the first item that follows it, so a
call in which nothing was said stores nothing. The user's open turn, and a reply that
can still be cut, are never stored. The database is the truth: if what the call settled
cannot be stored, the call is not to carry on unsaved, and fails. Everything settled is
stored by the time the call is closed, and only then is the gem let go.

Two kinds of "no" are told apart, because the app is told them apart:

- a refusal (`CallRefused`): the call was not opened because of what was asked, such as
  a gem that is not there, a gem already in a call, or a model whose provider has no
  key. Asking again the same way gets the same answer while that holds.
- a fault (`CallFault`): the backend could not open the call, or could not keep it, as
  when a gem's stored config cannot be read, a speech service is out of reach, or the
  conversation cannot be stored. It says which in words, for the app, and carries the
  failure itself.
"""

import asyncio
from collections.abc import AsyncIterator

from bson import ObjectId
from pydantic import ValidationError

from app.core.errors import NotFoundError
from app.entities.conversation_ent import ConversationEntityClass, Stored
from app.entities.darbar_ent import DarbarEntityClass
from app.entities.gem_ent import GemEntityClass
from app.entities.history import mode_line, settled
from app.entities.voice_ent import VoiceConversation, VoiceEntityClass, VoiceEvent, VoiceLine

__all__ = ["CallEntityClass", "CallFault", "CallRefused", "OpenCall"]


class CallRefused(Exception):
    """The call was not opened because of what was asked. `reason` says why, in words."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CallFault(Exception):
    """The call could not be opened, or kept, through a fault of ours.

    `reason` says what could not be done, in words. `cause` is the failure itself.
    """

    def __init__(self, reason: str, cause: Exception) -> None:
        super().__init__(reason)
        self.reason = reason
        self.cause = cause


class OpenCall:
    """A call with a gem, from when it takes hold of the gem until it lets it go.

    It holds the voice conversation, and stores what the conversation settles after the
    gem's stored conversation. `keep` stores as the call goes; `close` ends the call,
    stores the rest, and lets the gem go.
    """

    def __init__(self, conversations: ConversationEntityClass, key: str, gem_id: ObjectId) -> None:
        self._conversations = conversations
        self._key = key
        self._gem_id = gem_id
        self._conversation: VoiceConversation | None = None
        # The gem's conversation as stored, with what this call has stored of its own.
        self._stored = Stored([])
        # How many of the items the conversation added are stored.
        self._kept = 0
        self._storing = asyncio.Lock()
        # The latest write `keep` started. It is not stopped when `keep` is, so a write
        # the database may already have taken is never cut off half-way.
        self._writing: asyncio.Future[None] | None = None
        self._let_go = False

    def begin(self, conversation: VoiceConversation, stored: Stored) -> None:
        """The call's conversation is open, on the gem's conversation as `stored`."""
        self._conversation = conversation
        self._stored = stored

    def results_came(self) -> None:
        """A late result of the gem's has come in: it is told in the call."""
        if self._conversation is not None:
            self._conversation.results_came()

    async def send_audio(self, pcm: bytes) -> None:
        await self._open().send_audio(pcm)

    def set_output_delay(self, seconds: float) -> None:
        self._open().set_output_delay(seconds)

    def outputs(self) -> AsyncIterator[VoiceEvent | bytes]:
        return self._open().outputs()

    async def keep(self) -> None:
        """Store what the call settles, as it settles, for as long as the call is open.

        Raises `CallFault` if it cannot be stored.
        """
        conversation = self._open()
        while True:
            await conversation.grew.wait()
            conversation.grew.clear()
            self._writing = asyncio.ensure_future(self._store())
            await asyncio.shield(self._writing)

    async def close(self) -> None:
        """End the call: close its conversation, store what it settled, let the gem go.

        Raises `CallFault` if what it settled could not be stored. The gem is let go
        however the call ends.
        """
        try:
            if self._conversation is not None:
                try:
                    await self._conversation.close()
                finally:
                    await self._store_last()
        finally:
            self.let_go()

    def let_go(self) -> None:
        """The gem may be written to again, and its late results are told in writing."""
        if not self._let_go:
            self._let_go = True
            self._conversations.call_ended(self._key, self)

    async def _store_last(self) -> None:
        """Store the rest of what the call settled, once it is closed."""
        conversation = self._open()
        if self._writing is not None:
            # A write `keep` started finishes first. If it failed, what it was writing
            # is tried again below.
            await asyncio.gather(self._writing, return_exceptions=True)
        try:
            await self._store()
        except CallFault:
            # The placeholders of the calls it left running are in no stored
            # conversation: nothing could take their results.
            unstored = {
                added.item["call_id"]
                for added in conversation.added[self._kept :]
                if "call_id" in added.item and added.mark == "placeholder"
            }
            calls = [call for call in conversation.left_running if call.call_id in unstored]
            self._conversations.gem(self._key).late.forget(calls)
            raise
        if await self._conversations.gone(self._gem_id):
            # The gem was deleted during the call, and its conversation with it.
            self._conversations.gem(self._key).late.forget(conversation.left_running)

    async def _store(self) -> None:
        """Store what the conversation has settled and is not stored yet, in order."""
        conversation = self._open()
        async with self._storing:
            # A line of ours waits for the first item that follows it.
            ready = settled(conversation.added)
            if ready <= self._kept:
                return
            try:
                self._stored = await self._conversations.add(
                    self._gem_id, self._stored, conversation.added[self._kept : ready], "call"
                )
            except Exception as exc:
                raise CallFault("the conversation could not be stored", exc) from exc
            self._kept = ready

    def _open(self) -> VoiceConversation:
        if self._conversation is None:
            raise RuntimeError("the call's conversation is not open")
        return self._conversation


class CallEntityClass:
    def __init__(
        self,
        *,
        gems: GemEntityClass,
        darbar: DarbarEntityClass,
        voice: VoiceEntityClass,
        conversations: ConversationEntityClass,
    ) -> None:
        self._gems = gems
        self._darbar = darbar
        self._voice = voice
        self._conversations = conversations

    async def open(self, gem: str, *, speaks_first: bool | None = None) -> OpenCall:
        """Open a call with a gem. The caller owns the call and must `close()` it.

        `gem` is the gem's id. Its config says which model answers. `speaks_first` wins
        over the gem's when it is given; left out, it is the gem's to say.

        Raises `CallRefused` for a gem that is not there (or an id that is not an id),
        for a gem already in a call, and for a model whose provider has no key. Anything
        else that goes wrong is raised as a `CallFault`, which says what could not be
        read or reached.
        """
        try:
            config = await self._gems.get(gem)
        except NotFoundError:
            raise CallRefused("unknown gem") from None
        except ValidationError as exc:
            # The gem is there, but its stored config cannot be read here, as when a
            # newer cortex wrote it.
            raise CallFault("the gem's config cannot be read", exc) from exc
        except Exception as exc:
            raise CallFault("the gem could not be read", exc) from exc
        try:
            # Read once, here, so the call keeps the text it opened with.
            darbar = (await self._darbar.read()).text
        except Exception as exc:
            raise CallFault("the Darbar text could not be read", exc) from exc
        try:
            line = await self._voice.connect(config, speaks_first=speaks_first)
        except LookupError as exc:
            # The gem's model is offered, but its provider has no key here.
            raise CallRefused(str(exc)) from exc
        except Exception as exc:
            # A speech service is unreachable or not configured.
            raise CallFault("speech services unavailable", exc) from exc
        try:
            return await self._take(config.id, line, darbar)
        except BaseException:
            await line.close()
            raise

    async def _take(self, gem_id: ObjectId, line: VoiceLine, darbar: str | None) -> OpenCall:
        """Take hold of the gem, and open the call on its stored conversation.

        Everything that can refuse the call has been asked by now, so a reply being
        written to the gem is cut only for a call that opens, unless something fails.
        """
        key = str(gem_id)
        kept = self._conversations.gem(key)
        # Whether the gem is free and the taking of it, with no `await` in between.
        if kept.call is not None:
            raise CallRefused("the gem is in a call")
        call = OpenCall(self._conversations, key, gem_id)
        kept.call = call
        try:
            # A reply being written is cut, as a new message cuts it, and stored first.
            if (writer := kept.writer) is not None:
                writer.cut()
                await writer.over.wait()
            try:
                stored = await self._conversations.load(gem_id)
            except Exception as exc:
                raise CallFault("the conversation could not be read", exc) from exc
            if kept.turn.open:
                # A written turn left open has been sent to the model: the call follows it.
                added = [*mode_line("chat", stored.mode), *kept.turn.close()]
                try:
                    stored = await self._conversations.add(gem_id, stored, added, "chat")
                except Exception as exc:
                    raise CallFault("the conversation could not be stored", exc) from exc
            # Nothing is awaited after this, so a caller that cancels the opening either
            # gets the call back, to close, or none was made.
            conversation = line.open(
                darbar=darbar, history=stored.items, last_mode=stored.mode, late=kept.late
            )
        except BaseException:
            call.let_go()
            raise
        call.begin(conversation, stored)
        return call
