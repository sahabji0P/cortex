from pathlib import Path

from fastapi import APIRouter, WebSocket
from fastapi.responses import FileResponse

from app.routers.voice.service import VoiceServiceRegistry

router = APIRouter(prefix="/v1/voice", tags=["voice"])

_TEST_PAGE = Path(__file__).parent / "test_page.html"


@router.websocket("/converse")
async def converse(websocket: WebSocket, service: VoiceServiceRegistry.V1) -> None:
    """A spoken conversation over one WebSocket.

    Send mic audio as binary frames: raw 16-bit mono PCM at 16 kHz, about 100 ms each.
    Receive speech as binary frames (raw 16-bit mono PCM at 24 kHz) and JSON text frames
    `{"type": ..., "text": ...}` with type `user.partial`, `user.final`,
    `assistant.delta`, `assistant.done`, `tool.call`, `interrupt` or `error`.
    On `interrupt`, stop playback and drop any audio not yet played.
    """
    await service.converse(websocket)


@router.get("/test", include_in_schema=False)
async def test_page() -> FileResponse:
    """A one-file browser page for trying the conversation with a mic and speakers."""
    return FileResponse(_TEST_PAGE, media_type="text/html")
