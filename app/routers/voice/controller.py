from pathlib import Path

from fastapi import APIRouter, WebSocket
from fastapi.responses import FileResponse

from app.routers.voice.models import VoiceModels
from app.routers.voice.service import VoiceServiceRegistry

router = APIRouter(prefix="/v1/voice", tags=["voice"])

_TEST_PAGE = Path(__file__).parent / "test_page.html"


@router.websocket("/converse")
async def converse(websocket: WebSocket, service: VoiceServiceRegistry.V1) -> None:
    """A spoken conversation over one WebSocket.

    Add `?gem=` with a gem's id to talk to that gem: its config says who answers, with
    which model and tools, and whether it speaks first. Add `?model=` with a name from
    `GET /v1/voice/models` to choose the model; without it the gem's or the configured
    one is used. Add `?first=assistant` to have the assistant greet the user as soon as
    the socket opens, or `?first=user` to have it wait; without it, it does what the gem
    says, and without a gem it waits for the user to speak.
    Send mic audio as binary frames: raw 16-bit mono PCM at 16 kHz, about 100 ms each.
    Optionally send `{"type": "playback", "delay": seconds}` as a text frame to report how
    long your sound device delays audio, so interruptions are judged by what was heard.
    Receive speech as binary frames (raw 16-bit mono PCM at 24 kHz) and JSON text frames
    `{"type": ..., "text": ...}` with type `user.partial`, `user.final`,
    `assistant.delta`, `assistant.done`, `tool.call`, `interrupt` or `error`.
    On `interrupt`, stop playback and drop any audio not yet played.
    """
    await service.converse(websocket)


@router.get("/models")
async def models(service: VoiceServiceRegistry.V1) -> list[VoiceModels.V1.Output.Model]:
    """The models a conversation can be held with, and which of them have a key set."""
    return service.models()


@router.get("/test", include_in_schema=False)
async def test_page() -> FileResponse:
    """A one-file browser page for trying the conversation with a mic and speakers."""
    return FileResponse(_TEST_PAGE, media_type="text/html")
