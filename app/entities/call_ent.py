"""
Opening a call, and keeping what it adds to the gem's conversation.

A call is always with a gem, and may say who speaks first. Opening it reads the gem's
config, the Darbar text and the tools of the gem's connections, connects speech
recognition, then takes hold of the gem: a gem has one conversation and one writer at a
time (see `conversation_ent.py`). A reply being written to the gem is cut, as a new
message cuts it, and the call opens once that reply is stored. The call then starts from
the gem's conversation as it is stored, and adds its turns to it. The standing socket
(`app/routers/link`) holds the call from there.

What the call adds is stored as it is added, in order, after what was stored before:
our own lines, what the user says, the gem's replies as they were heard, and tool calls
with their results. A reply that can still be cut is never stored. The database is the
truth: if what the call added cannot be stored, the call is not to carry on unsaved,
and fails. Everything it added is stored by the time the call is closed, and only then
is the gem let go.

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
from collections.abc import AsyncIterator, Sequence

from bson import ObjectId
from pydantic import ValidationError

from app.core.errors import NotFoundError
from app.entities.connection_ent import ConnectionEntityClass
from app.entities.conversation_ent import ConversationEntityClass
from app.entities.darbar_ent import DarbarEntityClass
from app.entities.gem_ent import GemEntityClass
from app.entities.gem_log import Recording
from app.entities.history import TimeTold
from app.entities.tools import Tool
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

    It holds the voice conversation. What the conversation adds is handed to the gem's
    log as it is added, and written behind the call's back: `keep` sees to it that it is
    stored as the call goes, and fails the call if it cannot be. `close` ends the call,
    waits for the rest to be stored, and lets the gem go.
    """

    def __init__(self, conversations: ConversationEntityClass, key: str, gem_id: ObjectId) -> None:
        self._conversations = conversations
        self._key = key
        self._gem_id = gem_id
        self._conversation: VoiceConversation | None = None
        # The latest wait for a write that `keep` started. It is not stopped when `keep`
        # is, so a write the database may already have taken is never cut off half-way.
        self._writing: asyncio.Future[None] | None = None
        self._let_go = False
        self._record: Recording | None = None

    def begin(self, conversation: VoiceConversation, record: Recording | None = None) -> None:
        """The call's conversation is open, on the gem's conversation as it is stored.

        `record` is where the call's end is written down, if anywhere.
        """
        self._conversation = conversation
        self._record = record

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
        """See that what the call adds is stored, as it is added, for as long as it is open.

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
                    try:
                        await self._store_last()
                    finally:
                        # Last, after what the call settled: the call is over.
                        if self._record is not None:
                            self._record.call_closed()
                            self._record = None
        finally:
            self.let_go()

    def let_go(self) -> None:
        """The gem may be written to again, and its late results are told in writing."""
        if not self._let_go:
            self._let_go = True
            self._conversations.call_ended(self._key, self)

    async def _store_last(self) -> None:
        """Wait for the rest of what the call added to be stored, once it is closed."""
        conversation = self._open()
        if self._writing is not None:
            # A wait `keep` started finishes first. If the write it waited for failed,
            # it is tried again below.
            await asyncio.gather(self._writing, return_exceptions=True)
        try:
            await self._store()
        except BaseException:
            # The call fails for it, or this was stopped, and nothing tries again: what
            # could not be stored is dropped. The placeholders among it are in no stored
            # conversation, so nothing could take the results of the calls they stand for.
            dropped = self._conversations.log.give_up(self._gem_id)
            unstored = {
                entry.data["item"]["call_id"]
                for entry in dropped
                if entry.kind == "tool.result" and entry.data.get("mark") == "placeholder"
            }
            calls = [call for call in conversation.left_running if call.call_id in unstored]
            self._conversations.gem(self._key).late.forget(calls)
            raise
        if await self._conversations.gone(self._gem_id):
            # The gem was deleted during the call, and its conversation with it.
            self._conversations.gem(self._key).late.forget(conversation.left_running)

    async def _store(self) -> None:
        """Wait until what the conversation has added so far is stored."""
        try:
            await self._conversations.log.stored(self._gem_id)
        except Exception as exc:
            raise CallFault("the conversation could not be stored", exc) from exc

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
        connections: ConnectionEntityClass | None = None,
    ) -> None:
        self._gems = gems
        self._darbar = darbar
        self._voice = voice
        self._conversations = conversations
        # The tool servers gems can use. Without it, no gem is offered a connection's tools.
        self._connections = connections

    async def open(
        self, gem: str, *, speaks_first: bool | None = None, time_zone: str | None = None
    ) -> OpenCall:
        """Open a call with a gem. The caller owns the call and must `close()` it.

        `gem` is the gem's id. Its config says which model answers. `speaks_first` wins
        over the gem's when it is given; left out, it is the gem's to say. `time_zone` is
        the user's, as the app sent it: the call's line that gives the time is in it.

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
            # Read once, here: the tools a call offers are the same from start to end.
            # A tool closed to the gem during the call is refused when it is called.
            connected = (
                [] if self._connections is None else await self._connections.tools_for(config.id)
            )
        except Exception as exc:
            raise CallFault("the gem's connections could not be read", exc) from exc
        try:
            line = await self._voice.connect(config, speaks_first=speaks_first)
        except LookupError as exc:
            # The gem's model is offered, but its provider has no key here.
            raise CallRefused(str(exc)) from exc
        except Exception as exc:
            # A speech service is unreachable or not configured.
            raise CallFault("speech services unavailable", exc) from exc
        try:
            return await self._take(config.id, line, darbar, connected, time_zone)
        except BaseException:
            await line.close()
            raise

    async def _take(
        self,
        gem_id: ObjectId,
        line: VoiceLine,
        darbar: str | None,
        connected: Sequence[Tool],
        time_zone: str | None,
    ) -> OpenCall:
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
            # What happens in the call is written down in the gem's log.
            record = self._conversations.recording(gem_id, "call")
            record.call_opened()
            try:
                # Nothing is awaited after this, so a caller that cancels the opening
                # either gets the call back, to close, or none was made.
                conversation = line.open(
                    darbar=darbar,
                    history=stored.items,
                    last_mode=stored.mode,
                    late=kept.late,
                    connected=connected,
                    record=record,
                    time_told=TimeTold(time_zone),
                )
            except BaseException:
                # It did not open after all: the log does not leave it open.
                record.call_closed()
                raise
        except BaseException:
            call.let_go()
            raise
        call.begin(conversation, record)
        return call
