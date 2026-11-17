import asyncio
import contextlib
from functools import cache
from typing import Annotated

from fastapi import Depends, WebSocket
from pydantic import BaseModel, ValidationError

from app.entities import EntitiesRegistry
from app.entities.chat_ent import ChatEntityClass, ChatTurn
from app.routers.link.models import LinkModels

# How long one frame may take to be taken by the client before the client counts as gone.
_SEND_SECONDS = 10.0


class LinkServiceV1Class:
    def __init__(self, chat: ChatEntityClass | None = None) -> None:
        # Tests hand in their own. The app uses the one the registry made at startup.
        self.chat = EntitiesRegistry.Chat if chat is None else chat

    async def link(self, websocket: WebSocket) -> None:
        """Serve one standing socket until the client drops it.

        Every frame is JSON text with a `type`. The client sends `chat.send`. Replies
        are written concurrently, one per gem, and each one's frames go to the socket
        that asked for it. A socket that closes cuts its replies still being written.
        They are stored all the same, as cut.
        """
        await websocket.accept()
        # Replies to different gems write to the socket at once, one frame at a time.
        sending = asyncio.Lock()

        async def send(frame: BaseModel) -> None:
            async with sending:
                try:
                    async with asyncio.timeout(_SEND_SECONDS):
                        await websocket.send_text(frame.model_dump_json())
                except TimeoutError:
                    # A client that is connected but has stopped reading. Left alone, it
                    # would hold up its gem's next message, from any socket, and every
                    # other reply on this one. So it is treated as gone.
                    with contextlib.suppress(Exception):
                        await websocket.close()
                    raise

        turns: list[ChatTurn] = []
        try:
            await send(LinkModels.V1.Output.Ready())
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                if (text := message.get("text")) is None:
                    await send(LinkModels.V1.Output.Error(text="a frame must be JSON text"))
                    continue
                try:
                    asked = LinkModels.V1.Input.ChatSend.model_validate_json(text)
                except ValidationError as exc:
                    await send(LinkModels.V1.Output.Error(text=_reason(exc)))
                    continue
                turns = [turn for turn in turns if not turn.over.is_set()]
                turns.append(self.chat.send(asked.gem, asked.text, send))
        finally:
            for turn in turns:
                turn.cut()
            # Each reply is stored before its turn is over, so a socket that closes
            # returns once its replies are stored. If this handler is itself cancelled,
            # it stops waiting, but the turns run on in tasks of their own and are
            # stored all the same, unless the whole process ends first.
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(asyncio.gather(*(turn.over.wait() for turn in turns)))
            with contextlib.suppress(Exception):
                await websocket.close()


def _reason(exc: ValidationError) -> str:
    """Why a frame could not be read, in a line."""
    first = exc.errors()[0]
    where = ".".join(str(part) for part in first["loc"])
    return f"the frame could not be read: {first['msg']}" + (f" ({where})" if where else "")


# Cached, so every socket gets the one service, and with it the one chat entity that
# keeps each gem to one reply at a time.
@cache
def get_link_service_v1() -> LinkServiceV1Class:
    return LinkServiceV1Class()


class LinkServiceRegistry:
    type V1 = Annotated[LinkServiceV1Class, Depends(get_link_service_v1)]
