from fastapi import APIRouter, WebSocket

from app.routers.link.service import LinkServiceRegistry

# No sign-in, like the voice routes: this backend has one user for now.
router = APIRouter(prefix="/v1/link", tags=["link"])


@router.websocket("")
async def link(websocket: WebSocket, service: LinkServiceRegistry.V1) -> None:
    """The app's standing socket. It carries written chat with the gems.

    The server sends `{"type": "link.ready", "version": 1}` once it is open. Send
    `{"type": "chat.send", "gem": <id>, "text": ...}` to write to a gem: a reply still
    being written to that gem is cut first. The reply comes as `chat.text` and
    `chat.tool` frames, then exactly one of `chat.done`, `chat.cut` or `chat.error`,
    each with `gem` and `text`. A frame that cannot be read gets `{"type": "error",
    "text": ...}`, and the socket stays open. See `docs/chat-contract.md`.
    """
    await service.link(websocket)
