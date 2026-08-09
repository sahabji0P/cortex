from fastapi import APIRouter, WebSocket
from fastapi.responses import StreamingResponse

from app.adapters.cartesia_stt_adp import STTAutoFinalizeModel, STTEncoding
from app.routers.cortex.models import CortexModels
from app.routers.cortex.service import CortexServiceRegistry

router = APIRouter(prefix="/v1/cortex", tags=["cortex"])

# Disable proxy/client buffering so chunks reach the client as they're produced.
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.post("/projects", status_code=201)
async def create_project(
    payload: CortexModels.V1.Input.CreateProject,
    service: CortexServiceRegistry.V1,
) -> CortexModels.V1.Output.Project:
    return await service.create_project(payload)


@router.get("/projects")
async def list_projects(
    service: CortexServiceRegistry.V1,
) -> list[CortexModels.V1.Output.Project]:
    return await service.list_projects()


@router.post("/projects/{project_id}/sessions", status_code=201)
async def create_project_session(
    project_id: str,
    service: CortexServiceRegistry.V1,
) -> CortexModels.V1.Output.Session:
    return await service.create_session(project_id)


@router.get("/projects/{project_id}/sessions")
async def list_project_sessions(
    project_id: str,
    service: CortexServiceRegistry.V1,
) -> list[CortexModels.V1.Output.Session]:
    return await service.list_project_sessions(project_id)


@router.post("/sessions", status_code=201)
async def create_session(
    payload: CortexModels.V1.Input.CreateSession,
    service: CortexServiceRegistry.V1,
) -> CortexModels.V1.Output.Session:
    return await service.create_session(payload.project_id)


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: str,
    service: CortexServiceRegistry.V1,
) -> CortexModels.V1.Output.Session:
    return await service.get_session(session_id)


@router.patch("/sessions/{session_id}")
async def update_session(
    session_id: str,
    payload: CortexModels.V1.Input.UpdateSession,
    service: CortexServiceRegistry.V1,
) -> CortexModels.V1.Output.Session:
    return await service.update_session(session_id, payload)


@router.get("/sessions/{session_id}/turns")
async def get_session_turns(
    session_id: str,
    service: CortexServiceRegistry.V1,
) -> list[CortexModels.V1.Output.SessionTurn]:
    return await service.get_session_turns(session_id)


@router.post("/listen")
async def listen(
    payload: CortexModels.V1.Input.Listen,
    service: CortexServiceRegistry.V1,
) -> StreamingResponse:
    """Stream model output as Server-Sent Events.

    Each event: `event: <delta|info|source|image|done|error>` with a JSON `StreamChunk`
    data line, plus periodic `: heartbeat` keep-alive frames during idle gaps.
    Consume with a fetch/stream reader (not EventSource — this is POST).
    """
    session_id = await service.resolve_listen_session(payload)
    return StreamingResponse(
        service.stream(payload, session_id=session_id),
        media_type="text/event-stream",
        headers={**_SSE_HEADERS, "X-Session-ID": session_id},
    )


@router.websocket("/transcribe")
async def transcribe(
    websocket: WebSocket,
    service: CortexServiceRegistry.V1,
    model: STTAutoFinalizeModel | None = None,
    encoding: STTEncoding | None = None,
    sample_rate: int | None = None,
) -> None:
    """Realtime speech-to-text with turn detection over WebSocket.

    Connect with optional `model`, `encoding`, and `sample_rate` query parameters
    (defaults: ink-2, pcm_s16le, 16000). Send raw audio as binary frames (~100 ms
    chunks) and JSON text frames for commands: `{"type": "config", "turn": {...}}`
    to retune turn detection, `{"type": "close"}` to flush and finalize. Receive
    Cartesia turn events (connected, turn.start, turn.update, turn.eager_end,
    turn.resume, turn.end, error) as JSON text frames.
    """
    await service.transcribe(
        websocket,
        model=model,
        encoding=encoding,
        sample_rate=sample_rate,
    )
