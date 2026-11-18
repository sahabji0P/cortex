from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field

from app.entities.model import ModelName

# The whole stored conversation is sent to the model with every message, for good. A
# limit keeps one stray paste from riding along in every request after it.
_MESSAGE_MAX_LENGTH = 20_000


def _not_blank(value: str) -> str:
    # Refused rather than stored: the conversation is only ever added to, and a model
    # may refuse a request with an empty message in it, on every message after.
    if not value.strip():
        raise ValueError("A message cannot be blank")
    return value


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

            class CallStart(BaseModel):
                """Open a call. Anything else in the frame is ignored.

                `gem` is the id of the gem to talk to; without one the call is with
                Cortex. `model` is a name from `GET /v1/voice/models`, and `first` says
                who says the first words. A `model` or `first` that is sent wins over the
                gem's for this call; one left out, or sent as null, leaves the gem's.
                Without a gem they are the configured model and the user.
                """

                type: Literal["call.start"]
                gem: str | None = None
                model: ModelName | None = None
                first: Literal["user", "assistant"] | None = None

            class CallPlayback(BaseModel):
                """How long the app's sound device delays audio before it is heard.

                It lets the backend work out what the user has really heard when they
                interrupt.
                """

                type: Literal["call.playback"]
                delay: float = Field(ge=0)

        class Output:
            class Ready(BaseModel):
                """Sent once, as soon as the socket is open."""

                type: Literal["link.ready"] = "link.ready"
                # 2: the link carries calls as well as chat.
                # 3: a search carries on after a cut, and its result is told later.
                version: Literal[3] = 3

            class CallReady(BaseModel):
                """The call is open. Its audio and events follow."""

                type: Literal["call.ready"] = "call.ready"

            class CallEvent(BaseModel):
                """One of the call's `VoiceEvent`s, with `call.` put before its type."""

                type: Literal[
                    "call.user.partial",
                    "call.user.final",
                    "call.assistant.delta",
                    "call.assistant.done",
                    "call.tool.call",
                    "call.interrupt",
                    "call.error",
                ]
                text: str = ""

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
