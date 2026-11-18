from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field

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
            class ChatSend(BaseModel):
                """A message to a gem. Anything else in the frame is ignored."""

                type: Literal["chat.send"]
                gem: str  # the gem's id
                text: Annotated[
                    str, Field(max_length=_MESSAGE_MAX_LENGTH), AfterValidator(_not_blank)
                ]

        class Output:
            class Ready(BaseModel):
                """Sent once, as soon as the socket is open."""

                type: Literal["link.ready"] = "link.ready"
                version: Literal[1] = 1

            class Error(BaseModel):
                """A frame the backend could not read. The socket stays open."""

                type: Literal["error"] = "error"
                text: str
