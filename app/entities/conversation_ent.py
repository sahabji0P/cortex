import asyncio
import contextlib
from collections import defaultdict
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar, Literal

from bson import ObjectId
from bson.errors import InvalidId
from pydantic import BaseModel, ConfigDict, Field
from pydantic_mongo import PydanticObjectId

from app.adapters.docdb_adp import DocDBAdapterClass
from app.core.errors import ConflictError, NotFoundError


class _MongoModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PydanticObjectId = Field(default_factory=ObjectId, alias="_id")


class Project(_MongoModel):
    __collection_name__: ClassVar[str] = "Projects"

    name: str
    created_at: datetime
    updated_at: datetime


class Session(_MongoModel):
    __collection_name__: ClassVar[str] = "Sessions"

    project_id: PydanticObjectId | None = None
    has_turns: bool = False
    created_at: datetime
    updated_at: datetime


class SessionTurn(_MongoModel):
    __collection_name__: ClassVar[str] = "SessionTurns"

    provider: str
    parts: list[dict[str, str]] = Field(default_factory=list)
    # Legacy SessionTurns predate request-level fan-out grouping. Keep the field
    # nullable on reads instead of manufacturing a different id every time an old
    # document is deserialized; all newly-created turns still provide a real id.
    request_id: PydanticObjectId | None = None
    provider_response_id: str | None = None
    provider_parent_response_id: str | None = None
    created_at: datetime
    updated_at: datetime
    session_id: PydanticObjectId


@dataclass(frozen=True, slots=True)
class ContextMessage:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class SessionContext:
    messages: tuple[ContextMessage, ...]
    previous_response_id: str | None = None


def _object_id(value: str, resource: str) -> PydanticObjectId:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError) as err:
        raise NotFoundError(resource) from err


class ConversationEntityClass:
    """Manage Projects, Sessions, and their required SessionTurns."""

    def __init__(self, docdb: DocDBAdapterClass) -> None:
        self._docdb = docdb
        self._session_locks: dict[str, tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def session_run_lock(self, session_id: str) -> AsyncGenerator[None]:
        """Serialize model runs for one session within this application process."""
        entry = self._session_locks.get(session_id)
        if entry is None:
            lock = asyncio.Lock()
            references = 1
        else:
            lock, references = entry
            references += 1
        self._session_locks[session_id] = (lock, references)

        try:
            async with lock:
                yield
        finally:
            current_lock, current_references = self._session_locks[session_id]
            if current_references == 1:
                del self._session_locks[session_id]
            else:
                self._session_locks[session_id] = (current_lock, current_references - 1)

    async def create_project(self, name: str) -> Project:
        now = datetime.now(UTC)
        return await self._docdb.insert(Project(name=name, created_at=now, updated_at=now))

    async def list_projects(self) -> list[Project]:
        return await self._docdb.get_many(
            Project,
            filters={},
            sort=[("updated_at", -1)],
        )

    async def _get_project(self, project_id: str) -> Project:
        project = await self._docdb.get(
            Project,
            filters={"id": _object_id(project_id, "Project")},
        )
        if project is None:
            raise NotFoundError("Project")
        return project

    async def create_session(self, project_id: str | None = None) -> Session:
        resolved_project_id: PydanticObjectId | None = None
        if project_id is not None:
            project = await self._get_project(project_id)
            resolved_project_id = project.id
        now = datetime.now(UTC)
        return await self._docdb.insert(
            Session(
                project_id=resolved_project_id,
                created_at=now,
                updated_at=now,
            )
        )

    async def get_session(self, session_id: str) -> Session:
        session = await self._docdb.get(
            Session,
            filters={"id": _object_id(session_id, "Session")},
        )
        if session is None:
            raise NotFoundError("Session")
        return session

    async def list_project_sessions(self, project_id: str) -> list[Session]:
        project = await self._get_project(project_id)
        return await self._docdb.get_many(
            Session,
            filters={"project_id": project.id},
            sort=[("updated_at", -1)],
        )

    async def set_session_project(
        self,
        session_id: str,
        project_id: str | None,
    ) -> Session:
        session = await self.get_session(session_id)
        resolved_project_id: PydanticObjectId | None = None
        if project_id is not None:
            project = await self._get_project(project_id)
            resolved_project_id = project.id

        if session.project_id == resolved_project_id:
            return session

        updated_at = datetime.now(UTC)
        matched = await self._docdb.update(
            Session,
            {"id": session.id, "has_turns": False},
            {"project_id": resolved_project_id, "updated_at": updated_at},
        )
        if matched == 0:
            raise ConflictError("A session's project cannot change after its first turn")
        return session.model_copy(
            update={"project_id": resolved_project_id, "updated_at": updated_at}
        )

    async def create_turn(
        self,
        *,
        provider: str,
        session_id: str,
        request_id: PydanticObjectId,
        prompt: str,
        provider_parent_response_id: str | None = None,
    ) -> SessionTurn:
        resolved_session_id = _object_id(session_id, "Session")
        now = datetime.now(UTC)
        matched = await self._docdb.update(
            Session,
            {"id": resolved_session_id},
            {"has_turns": True, "updated_at": now},
        )
        if matched == 0:
            raise NotFoundError("Session")
        try:
            return await self._docdb.insert(
                SessionTurn(
                    provider=provider,
                    parts=[{"type": "user", "text": prompt}],
                    request_id=request_id,
                    provider_parent_response_id=provider_parent_response_id,
                    session_id=resolved_session_id,
                    created_at=now,
                    updated_at=now,
                )
            )
        except BaseException:
            # Mongo is intentionally usable as a standalone local instance, so a
            # multi-document transaction is not available. Compensate a failed first
            # insert only when no turn exists for this session; otherwise another
            # writer has legitimately started it and the membership lock must remain.
            with contextlib.suppress(Exception):
                if (
                    await self._docdb.count(
                        SessionTurn,
                        filters={"session_id": resolved_session_id},
                    )
                    == 0
                ):
                    await self._docdb.update(
                        Session,
                        {"id": resolved_session_id, "has_turns": True},
                        {"has_turns": False, "updated_at": datetime.now(UTC)},
                    )
            raise

    async def save_turn_parts_shielded(
        self,
        turn_id: PydanticObjectId,
        parts: list[dict[str, str]],
        provider_response_id: str | None = None,
    ) -> None:
        update_data: dict[str, object] = {
            "parts": parts,
            "updated_at": datetime.now(UTC),
        }
        if provider_response_id is not None:
            update_data["provider_response_id"] = provider_response_id
        task = asyncio.create_task(
            self._docdb.update(
                SessionTurn,
                {"id": turn_id},
                update_data,
            )
        )
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def get_session_turns(self, session_id: str) -> list[SessionTurn]:
        session = await self.get_session(session_id)
        return await self._docdb.get_many(
            SessionTurn,
            filters={"session_id": session.id},
            sort=[("created_at", 1)],
        )

    @staticmethod
    def build_context(
        turns: Sequence[SessionTurn],
        target_provider: str,
    ) -> SessionContext:
        """Build one normalized exchange per request, preferring the target provider."""
        grouped: dict[tuple[str, object], list[SessionTurn]] = defaultdict(list)
        legacy_group_number = 0
        legacy_prompt = ""
        legacy_providers: set[str] = set()
        for turn in turns:
            if turn.request_id is not None:
                key: tuple[str, object] = ("request", turn.request_id)
                legacy_prompt = ""
                legacy_providers.clear()
            else:
                prompt = ConversationEntityClass._part_text(turn.parts, "user")
                if not prompt or prompt != legacy_prompt or turn.provider in legacy_providers:
                    legacy_group_number += 1
                    legacy_prompt = prompt
                    legacy_providers.clear()
                legacy_providers.add(turn.provider)
                key = ("legacy", legacy_group_number)
            grouped[key].append(turn)

        messages: list[ContextMessage] = []
        groups = list(grouped.values())
        for group in groups:
            chosen = next(
                (
                    turn
                    for turn in group
                    if turn.provider == target_provider
                    and ConversationEntityClass._completed_response(turn) is not None
                ),
                None,
            )
            if chosen is None:
                chosen = next(
                    (
                        turn
                        for turn in group
                        if ConversationEntityClass._completed_response(turn) is not None
                    ),
                    None,
                )
            prompt = next(
                (
                    text
                    for turn in group
                    if (text := ConversationEntityClass._part_text(turn.parts, "user"))
                ),
                "",
            )
            response = (
                ConversationEntityClass._completed_response(chosen) if chosen is not None else None
            )
            if not prompt or response is None:
                continue
            messages.extend(
                (
                    ContextMessage(role="user", content=prompt),
                    ContextMessage(role="assistant", content=response),
                )
            )

        previous_response_id: str | None = None
        if groups:
            latest_target = next(
                (
                    turn
                    for turn in groups[-1]
                    if turn.provider == target_provider
                    and ConversationEntityClass._completed_response(turn) is not None
                ),
                None,
            )
            if latest_target is not None:
                previous_response_id = latest_target.provider_response_id
                parent_response_id = latest_target.provider_parent_response_id
                if parent_response_id is not None:
                    sibling_branches = sum(
                        1
                        for turn in turns
                        if turn.provider == target_provider
                        and turn.provider_parent_response_id == parent_response_id
                        and ConversationEntityClass._completed_response(turn) is not None
                    )
                    if sibling_branches > 1:
                        # Concurrent workers branched from the same provider-side
                        # continuation. Neither branch contains the other, so force the
                        # next request to rebuild from the complete Mongo transcript.
                        previous_response_id = None

        return SessionContext(
            messages=tuple(messages),
            previous_response_id=previous_response_id,
        )

    @staticmethod
    def _part_text(parts: Sequence[dict[str, str]], part_type: str) -> str:
        return "".join(part.get("text", "") for part in parts if part.get("type") == part_type)

    @staticmethod
    def _completed_response(turn: SessionTurn) -> str | None:
        if any(part.get("type") == "error" for part in turn.parts):
            return None
        if not any(part.get("type") == "done" for part in turn.parts):
            return None
        return ConversationEntityClass._part_text(turn.parts, "text") or None


__all__ = [
    "ContextMessage",
    "ConversationEntityClass",
    "Project",
    "Session",
    "SessionContext",
    "SessionTurn",
]
