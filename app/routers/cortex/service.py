import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, WebSocket
from pydantic import Field, TypeAdapter, ValidationError

from app.adapters.cartesia_stt_adp import (
    CartesiaSTTSession,
    STTAutoFinalizeModel,
    STTEncoding,
)
from app.entities import EntitiesRegistry
from app.entities.conversation_ent import Project, Session, SessionTurn
from app.entities.pipeline_llm import StreamChunk
from app.routers.cortex.models import CortexModels

# Seconds of idle (no chunk) before emitting a keep-alive frame.
_HEARTBEAT_INTERVAL = 15.0
# SSE comment frame: a line starting with ":" is ignored by clients but keeps the
# TCP connection (and any proxy in between) from timing out during quiet stretches.
_HEARTBEAT_FRAME = ": heartbeat\n\n"

# Parses client text frames on the transcribe WebSocket into typed commands.
_TRANSCRIBE_COMMAND_ADAPTER: TypeAdapter[
    CortexModels.V1.Input.TranscribeConfig | CortexModels.V1.Input.TranscribeClose
] = TypeAdapter(
    Annotated[
        CortexModels.V1.Input.TranscribeConfig | CortexModels.V1.Input.TranscribeClose,
        Field(discriminator="type"),
    ]
)


def _format_sse(chunk: StreamChunk) -> str:
    """Serialize a StreamChunk as one SSE frame: event name = chunk type, data = JSON."""
    return f"event: {chunk.type}\ndata: {chunk.model_dump_json()}\n\n"


def _error_frame(message: str) -> str:
    """Build a client-facing error frame matching Cartesia's `error` event shape."""
    return json.dumps({"type": "error", "message": message})


class CortexServiceV1Class:
    def __init__(self) -> None:
        self.pipeline = EntitiesRegistry.LLMPipeline
        self.conversations = EntitiesRegistry.Conversations
        self.stt = EntitiesRegistry.STT

    @staticmethod
    def _project_out(project: Project) -> CortexModels.V1.Output.Project:
        return CortexModels.V1.Output.Project(
            id=str(project.id),
            name=project.name,
            created_at=project.created_at,
            updated_at=project.updated_at,
        )

    @staticmethod
    def _session_out(session: Session) -> CortexModels.V1.Output.Session:
        return CortexModels.V1.Output.Session(
            id=str(session.id),
            project_id=str(session.project_id) if session.project_id is not None else None,
            has_turns=session.has_turns,
            created_at=session.created_at,
            updated_at=session.updated_at,
        )

    @staticmethod
    def _turn_out(turn: SessionTurn) -> CortexModels.V1.Output.SessionTurn:
        return CortexModels.V1.Output.SessionTurn(
            id=str(turn.id),
            provider=turn.provider,
            parts=turn.parts,
            created_at=turn.created_at,
            updated_at=turn.updated_at,
            session_id=str(turn.session_id),
        )

    async def create_project(
        self,
        payload: CortexModels.V1.Input.CreateProject,
    ) -> CortexModels.V1.Output.Project:
        project = await self.conversations.create_project(payload.name)
        return self._project_out(project)

    async def list_projects(self) -> list[CortexModels.V1.Output.Project]:
        return [self._project_out(project) for project in await self.conversations.list_projects()]

    async def create_session(
        self,
        project_id: str | None = None,
    ) -> CortexModels.V1.Output.Session:
        return self._session_out(await self.conversations.create_session(project_id))

    async def get_session(self, session_id: str) -> CortexModels.V1.Output.Session:
        return self._session_out(await self.conversations.get_session(session_id))

    async def resolve_listen_session(
        self,
        payload: CortexModels.V1.Input.Listen,
    ) -> str:
        """Resolve an existing session or create one for the first submitted turn."""
        if payload.session_id is not None:
            session = await self.conversations.get_session(payload.session_id)
        else:
            session = await self.conversations.create_session(payload.project_id)
        return str(session.id)

    async def list_project_sessions(
        self,
        project_id: str,
    ) -> list[CortexModels.V1.Output.Session]:
        sessions = await self.conversations.list_project_sessions(project_id)
        return [self._session_out(session) for session in sessions]

    async def update_session(
        self,
        session_id: str,
        payload: CortexModels.V1.Input.UpdateSession,
    ) -> CortexModels.V1.Output.Session:
        session = await self.conversations.set_session_project(session_id, payload.project_id)
        return self._session_out(session)

    async def get_session_turns(
        self,
        session_id: str,
    ) -> list[CortexModels.V1.Output.SessionTurn]:
        turns = await self.conversations.get_session_turns(session_id)
        return [self._turn_out(turn) for turn in turns]

    async def stream(
        self,
        payload: CortexModels.V1.Input.Listen,
        *,
        session_id: str,
    ) -> AsyncIterator[str]:
        """Stream pipeline chunks as SSE, injecting heartbeats during idle gaps.

        A producer task drains the pipeline into a queue; this consumer reads with a
        timeout so it can emit a keep-alive frame whenever no chunk arrives in time.
        """
        queue: asyncio.Queue[StreamChunk | None] = asyncio.Queue()

        async def _produce() -> None:
            try:
                async for chunk in self.pipeline.run(
                    payload.prompt,
                    payload.providers,
                    gemini_mode=payload.gemini_mode,
                    gemini_tools=payload.gemini_tools or None,
                    anthropic_model=payload.anthropic_model,
                    openai_thinking=payload.openai_thinking,
                    session_id=session_id,
                ):
                    await queue.put(chunk)
            except Exception as exc:
                # Failures outside the per-provider streams (e.g. the session was
                # deleted mid-request) must reach the client as an error frame, not
                # kill the response after SSE headers have been sent.
                await queue.put(
                    StreamChunk(provider=payload.providers[0], type="error", text=str(exc))
                )
            finally:
                await queue.put(None)  # sentinel: stream finished

        task = asyncio.create_task(_produce())
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_INTERVAL)
                except TimeoutError:
                    yield _HEARTBEAT_FRAME
                    continue
                if item is None:
                    break
                yield _format_sse(item)
        finally:
            # Client disconnected or stream ended — stop the producer cleanly.
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def transcribe(
        self,
        websocket: WebSocket,
        *,
        model: STTAutoFinalizeModel | None = None,
        encoding: STTEncoding | None = None,
        sample_rate: int | None = None,
    ) -> None:
        """Bridge a client WebSocket to a Cartesia realtime STT turns session.

        Uplink: client binary frames are raw audio; client text frames are JSON
        commands (`config` retunes turn detection, `close` flushes and finalizes).
        Downlink: every typed Cartesia event (connected, turn.start, turn.update,
        turn.eager_end, turn.resume, turn.end, error) is forwarded verbatim as one
        JSON text frame. The socket closes after Cartesia finalizes or either side
        drops.
        """
        await websocket.accept()
        try:
            session = await self.stt.open_turns_session(
                model=model,
                encoding=encoding,
                sample_rate=sample_rate,
            )
        except Exception:
            # 1011: internal error — Cartesia unreachable or the adapter is not configured.
            with contextlib.suppress(Exception):
                await websocket.close(code=1011, reason="speech-to-text upstream unavailable")
            raise

        uplink = asyncio.create_task(self._relay_client_frames(websocket, session))
        downlink = asyncio.create_task(self._relay_stt_events(websocket, session))
        try:
            done, _pending = await asyncio.wait(
                {uplink, downlink},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                # Surface unexpected relay failures to FastAPI's error handling/logs.
                if (exc := task.exception()) is not None:
                    raise exc
        finally:
            # asyncio.wait does NOT cancel its tasks if this coroutine is cancelled
            # (client abort, server shutdown) — clean up both relays and the upstream
            # socket unconditionally so nothing leaks.
            for task in (uplink, downlink):
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            with contextlib.suppress(Exception):
                await session.close()
            with contextlib.suppress(Exception):
                await websocket.close()

    @staticmethod
    async def _relay_client_frames(websocket: WebSocket, session: CartesiaSTTSession) -> None:
        """Pump client frames into Cartesia until the client disconnects."""
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            if (audio := message.get("bytes")) is not None:
                await session.send_audio(audio)
                continue
            if (text := message.get("text")) is None:
                continue
            try:
                command = _TRANSCRIBE_COMMAND_ADAPTER.validate_json(text)
            except ValidationError as exc:
                await websocket.send_text(_error_frame(f"invalid command frame: {exc}"))
                continue
            if command.type == "config":
                await session.update_turn_config(command.turn)
            else:
                # `close`: Cartesia flushes buffered audio, emits final turn events,
                # then closes its socket — which ends the downlink relay naturally.
                await session.close_input()

    async def _relay_stt_events(self, websocket: WebSocket, session: CartesiaSTTSession) -> None:
        """Forward typed Cartesia events to the client until Cartesia closes the socket."""
        async for event in session.events():
            await websocket.send_text(self.stt.serialize_event(event))


_singleton: CortexServiceV1Class | None = None


def get_cortex_service_v1() -> CortexServiceV1Class:
    global _singleton  # noqa: PLW0603
    if _singleton is None:
        _singleton = CortexServiceV1Class()
    return _singleton


class CortexServiceRegistry:
    type V1 = Annotated[CortexServiceV1Class, Depends(get_cortex_service_v1)]
