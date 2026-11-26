from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, BeforeValidator, Field

from app.entities.history import zone_named

# The whole stored conversation is sent to the model with every message, for good. A
# limit keeps one stray paste from riding along in every request after it.
_MESSAGE_MAX_LENGTH = 20_000


def _not_blank(value: str) -> str:
    # Refused rather than stored: the conversation is only ever added to, and a model
    # may refuse a request with an empty message in it, on every message after.
    if not value.strip():
        raise ValueError("A message cannot be blank")
    return value


def _zone_or_none(value: object) -> str | None:
    # The user's time zone is never a reason to refuse a frame: a name that cannot be
    # read, or anything else sent in its place, counts as none sent.
    zone = zone_named(value)
    return None if zone is None else zone.key


# An IANA name such as "Asia/Kolkata", or None. With none, the time is given in UTC.
type _TimeZone = Annotated[str | None, BeforeValidator(_zone_or_none)]


class LinkModels:
    class V1:
        class Input:
            class Frame(BaseModel):
                """Any frame, read for its `type` first. Its own model is read after."""

                type: str

            class ChatSend(BaseModel):
                """A message to a gem. Anything else in the frame is ignored."""

                type: Literal["chat.send"]
                gem: str  # the gem's id
                text: Annotated[
                    str, Field(max_length=_MESSAGE_MAX_LENGTH), AfterValidator(_not_blank)
                ]
                # The user's time zone, for the line that gives the time before the message.
                time_zone: _TimeZone = None

            class CallStart(BaseModel):
                """Open a call with a gem. Anything else in the frame is ignored.

                `gem` is the id of the gem to talk to. Its config says which model
                answers. `first` says who says the first words: sent, it wins over the
                gem's for this call; left out, or sent as null, it leaves the gem's.
                `time_zone` is the user's, for the line that gives the time as the call
                opens.
                """

                type: Literal["call.start"]
                gem: str
                first: Literal["user", "assistant"] | None = None
                time_zone: _TimeZone = None

            class CallPlayback(BaseModel):
                """How long the app's sound device delays audio before it is heard.

                It lets the backend work out what the user has really heard when they
                interrupt.
                """

                type: Literal["call.playback"]
                delay: float = Field(ge=0)

            class LogWatch(BaseModel):
                """Send this socket each entry of a gem's log as it is written down.

                `gem` is the gem's id. Left out, or null, every gem's log is sent.
                Nothing is sent of what was written down before: that is read with
                `GET /v1/gems/{id}/log`. `log.unwatch` with a gem's id stops that
                gem's, and with none stops them all, whatever was asked for.
                """

                type: Literal["log.watch", "log.unwatch"]
                gem: str | None = None

        class Output:
            class Ready(BaseModel):
                """Sent once, as soon as the socket is open."""

                type: Literal["link.ready"] = "link.ready"
                # 2: the link carries calls as well as chat.
                # 3: a search carries on after a cut, and its result is told later.
                # 4: a call's reply comes as words timed to its audio, not as text.
                # 5: a connection whose sign-in changes is told (`connection.changed`).
                # 6: a gem's log can be watched (`log.watch`, `log.entry`).
                # 7: an event a gem watches for can wake it (`chat.event`).
                version: Literal[7] = 7

            class LogEntry(BaseModel):
                """One entry of a gem's log, as it is written down, for a socket that watches it.

                `entry` has `seq`, `at`, `kind` and `turn`, then its own fields by kind
                (see `docs/chat-contract.md`). A long text or list in it is cut short
                here, and the entry then says `partial`: the whole of it is read with
                `GET /v1/gems/{id}/log/{seq}`. An entry is placed by its `seq`: an item
                can come after an entry with a higher number.
                """

                type: Literal["log.entry"] = "log.entry"
                gem: str
                entry: dict[str, Any]

            class ConnectionChanged(BaseModel):
                """A connection's sign-in has changed: read it again (`GET /v1/connections/{id}`).

                Sent to every socket when a connection comes to need a sign-in, as when
                the server refuses its pass and it cannot be renewed, and when a sign-in
                is finished. It says nothing else: what lets us in is never sent.
                """

                type: Literal["connection.changed"] = "connection.changed"
                id: str

            class CallReady(BaseModel):
                """The call is open. Its audio and events follow."""

                type: Literal["call.ready"] = "call.ready"

            class CallEvent(BaseModel):
                """One of the call's `VoiceEvent`s, with `call.` put before its type."""

                type: Literal[
                    "call.user.partial",
                    "call.user.final",
                    "call.assistant.done",
                    "call.tool.call",
                    "call.interrupt",
                    "call.error",
                ]
                text: str = ""

            class CallWord(BaseModel):
                """One word of a call's reply, as it is spoken.

                `start` is the sample the word begins at in the call's speech audio: at
                24 kHz, counted over all the speech sent since `call.ready`.
                """

                type: Literal["call.assistant.word"] = "call.assistant.word"
                text: str
                start: int

            class CallEnded(BaseModel):
                """The call is over: the app hung up, or the conversation ended by itself."""

                type: Literal["call.ended"] = "call.ended"
                text: str = ""

            class CallRefused(BaseModel):
                """The call was not opened because of what was asked. `text` says why."""

                type: Literal["call.refused"] = "call.refused"
                text: str

            class CallFailed(BaseModel):
                """The call could not be opened, or broke, through a fault of ours."""

                type: Literal["call.failed"] = "call.failed"
                text: str

            class Error(BaseModel):
                """A frame the backend could not read. The socket stays open."""

                type: Literal["error"] = "error"
                text: str
