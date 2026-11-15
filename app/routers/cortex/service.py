import asyncio
import contextlib
import json
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
from app.routers.cortex.models import CortexModels

# Parses client text frames on the transcribe WebSocket into typed commands.
_TRANSCRIBE_COMMAND_ADAPTER: TypeAdapter[
    CortexModels.V1.Input.TranscribeConfig | CortexModels.V1.Input.TranscribeClose
] = TypeAdapter(
    Annotated[
        CortexModels.V1.Input.TranscribeConfig | CortexModels.V1.Input.TranscribeClose,
        Field(discriminator="type"),
    ]
)


def _error_frame(message: str) -> str:
    """Build a client-facing error frame matching Cartesia's `error` event shape."""
    return json.dumps({"type": "error", "message": message})


class CortexServiceV1Class:
    def __init__(self) -> None:
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
