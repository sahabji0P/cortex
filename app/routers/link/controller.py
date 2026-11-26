from fastapi import APIRouter, WebSocket

from app.routers.link.service import LinkServiceRegistry

# No sign-in, like the voice routes: this backend has one user for now.
router = APIRouter(prefix="/v1/link", tags=["link"])


@router.websocket("")
async def link(websocket: WebSocket, service: LinkServiceRegistry.V1) -> None:
    """The app's standing socket. It carries written chat with the gems, and calls.

    The server sends `{"type": "link.ready", "version": 7}` once it is open.

    Chat: send `{"type": "chat.send", "gem": <id>, "text": ...}` to write to a gem: a
    reply still being written to that gem is cut first. The reply comes as `chat.text`
    and `chat.tool` frames, then exactly one of `chat.done`, `chat.cut` or `chat.error`,
    each with `gem` and `text`. A search that carries on after a cut is told later, in
    a reply of its own that starts with `chat.result`, sent to the socket of the latest
    message to that gem; or, if a message to that gem is waiting to be answered when it
    comes back, in the reply to that message.

    A call with a gem, one at a time: send `{"type": "call.start", "gem": <id>}`, with
    an optional `first`. Send mic audio as binary frames (raw 16-bit
    mono PCM at 16 kHz, about 100 ms each), `{"type": "call.playback", "delay": seconds}`
    for the sound device's delay, and `{"type": "call.end"}` to hang up. The call comes
    as `call.ready`, then speech as binary frames (raw 16-bit mono PCM at 24 kHz) and the
    call's events with `call.` before their type, then exactly one of `call.ended` or
    `call.failed`; or, if it never opened, `call.refused` or `call.failed`, or
    `call.ended` if it was hung up while it opened.

    A frame that cannot be read gets `{"type": "error", "text": ...}`, and the socket
    stays open. See `docs/chat-contract.md`.
    """
    await service.link(websocket)
