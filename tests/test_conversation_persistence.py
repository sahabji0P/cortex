import asyncio

import pytest
from bson import ObjectId
from pydantic import ValidationError

from app.core.errors import ConflictError, NotFoundError
from app.entities.conversation_ent import (
    ContextMessage,
    ConversationEntityClass,
    Project,
    Session,
    SessionTurn,
)
from app.routers.cortex.models import CortexModels


class _DocDBFake:
    def __init__(
        self,
        *,
        session: Session | None = None,
        project: Project | None = None,
        matched: int = 1,
    ) -> None:
        self.session = session
        self.project = project
        self.matched = matched
        self.update_filters: dict[str, object] | None = None
        self.inserted = False

    async def get(self, collection, *, filters):
        if collection is Session:
            return self.session
        if collection is Project:
            return self.project
        raise AssertionError(f"Unexpected collection: {collection}")

    async def update(self, _collection, filters, _update_data):
        self.update_filters = filters
        return self.matched

    async def insert(self, data):
        self.inserted = True
        return data


def _session(*, project_id: ObjectId | None, has_turns: bool = False) -> Session:
    return Session(
        project_id=project_id,
        has_turns=has_turns,
        created_at="2026-07-14T00:00:00Z",
        updated_at="2026-07-14T00:00:00Z",
    )


def _project() -> Project:
    return Project(
        name="Project A",
        created_at="2026-07-14T00:00:00Z",
        updated_at="2026-07-14T00:00:00Z",
    )


def _turn(
    *,
    provider: str,
    prompt: str,
    response: str,
    request_id: ObjectId | None,
    done: bool = True,
    error: str | None = None,
) -> SessionTurn:
    parts = [
        {"type": "user", "text": prompt},
        {"type": "text", "text": response},
    ]
    if error is not None:
        parts.append({"type": "error", "text": error})
    if done:
        parts.append({"type": "done"})
    return SessionTurn(
        provider=provider,
        parts=parts,
        request_id=request_id,
        session_id=ObjectId(),
        created_at="2026-07-14T00:00:00Z",
        updated_at="2026-07-14T00:00:00Z",
    )


def test_project_name_is_trimmed_and_cannot_be_blank() -> None:
    assert CortexModels.V1.Input.CreateProject(name="  Project A  ").name == "Project A"
    with pytest.raises(ValidationError):
        CortexModels.V1.Input.CreateProject(name="   ")


@pytest.mark.asyncio
async def test_project_change_uses_unstarted_session_filter() -> None:
    project = _project()
    session = _session(project_id=None)
    docdb = _DocDBFake(session=session, project=project)
    entity = ConversationEntityClass(docdb)  # type: ignore[arg-type]

    updated = await entity.set_session_project(str(session.id), str(project.id))

    assert updated.project_id == project.id
    assert docdb.update_filters == {"id": session.id, "has_turns": False}


@pytest.mark.asyncio
async def test_project_change_conflicts_when_session_started_concurrently() -> None:
    project = _project()
    session = _session(project_id=None)
    entity = ConversationEntityClass(
        _DocDBFake(session=session, project=project, matched=0)  # type: ignore[arg-type]
    )

    with pytest.raises(ConflictError):
        await entity.set_session_project(str(session.id), str(project.id))


@pytest.mark.asyncio
async def test_turn_cannot_be_created_without_existing_session() -> None:
    docdb = _DocDBFake(matched=0)
    entity = ConversationEntityClass(docdb)  # type: ignore[arg-type]

    with pytest.raises(NotFoundError):
        await entity.create_turn(
            provider="openai",
            session_id="507f1f77bcf86cd799439011",
            request_id=ObjectId(),
            prompt="hello",
        )
    assert docdb.inserted is False


@pytest.mark.asyncio
async def test_turn_starts_with_user_prompt_in_parts() -> None:
    session = _session(project_id=None)
    docdb = _DocDBFake(session=session)
    entity = ConversationEntityClass(docdb)  # type: ignore[arg-type]

    turn = await entity.create_turn(
        provider="openai",
        session_id=str(session.id),
        request_id=ObjectId(),
        prompt="persist this question",
    )

    assert turn.parts == [{"type": "user", "text": "persist this question"}]


@pytest.mark.asyncio
async def test_failed_turn_insert_releases_membership_lock_when_session_is_empty() -> None:
    session = _session(project_id=None)

    class _FailingDocDB(_DocDBFake):
        def __init__(self) -> None:
            super().__init__(session=session)
            self.updates: list[tuple[dict[str, object], dict[str, object]]] = []

        async def update(self, _collection, filters, update_data):
            self.updates.append((filters, update_data))
            return 1

        async def insert(self, _data):
            raise RuntimeError("insert failed")

        async def count(self, _collection, *, filters):
            assert filters == {"session_id": session.id}
            return 0

    docdb = _FailingDocDB()
    entity = ConversationEntityClass(docdb)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="insert failed"):
        await entity.create_turn(
            provider="openai",
            session_id=str(session.id),
            request_id=ObjectId(),
            prompt="hello",
        )

    assert docdb.updates[0][1]["has_turns"] is True
    assert docdb.updates[1][0] == {"id": session.id, "has_turns": True}
    assert docdb.updates[1][1]["has_turns"] is False


@pytest.mark.asyncio
async def test_session_run_lock_serializes_same_session_and_allows_other_sessions() -> None:
    entity = ConversationEntityClass(object())  # type: ignore[arg-type]
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()
    other_entered = asyncio.Event()

    async def first() -> None:
        async with entity.session_run_lock("one"):
            first_entered.set()
            await release_first.wait()

    async def second() -> None:
        await first_entered.wait()
        async with entity.session_run_lock("one"):
            second_entered.set()

    async def other() -> None:
        await first_entered.wait()
        async with entity.session_run_lock("two"):
            other_entered.set()

    tasks = [asyncio.create_task(coro()) for coro in (first, second, other)]
    await first_entered.wait()
    await asyncio.wait_for(other_entered.wait(), timeout=1)
    assert not second_entered.is_set()
    release_first.set()
    await asyncio.gather(*tasks)
    assert second_entered.is_set()
    assert entity._session_locks == {}


def test_context_groups_provider_fanout_and_prefers_target_provider() -> None:
    first_request = ObjectId()
    second_request = ObjectId()
    turns = [
        _turn(
            provider="gemini",
            prompt="first",
            response="gemini first",
            request_id=first_request,
        ),
        _turn(
            provider="openai",
            prompt="first",
            response="openai first",
            request_id=first_request,
        ),
        _turn(
            provider="gemini",
            prompt="second",
            response="gemini second",
            request_id=second_request,
        ),
    ]

    context = ConversationEntityClass.build_context(turns, "openai")

    assert context.messages == (
        ContextMessage(role="user", content="first"),
        ContextMessage(role="assistant", content="openai first"),
        ContextMessage(role="user", content="second"),
        ContextMessage(role="assistant", content="gemini second"),
    )


def test_context_falls_back_from_failed_target_and_ignores_partial_output() -> None:
    first_request = ObjectId()
    second_request = ObjectId()
    context = ConversationEntityClass.build_context(
        [
            _turn(
                provider="openai",
                prompt="first",
                response="",
                request_id=first_request,
                error="provider failed",
            ),
            _turn(
                provider="gemini",
                prompt="first",
                response="successful fallback",
                request_id=first_request,
            ),
            _turn(
                provider="openai",
                prompt="second",
                response="truncated",
                request_id=second_request,
                done=False,
            ),
        ],
        "openai",
    )

    assert context.messages == (
        ContextMessage(role="user", content="first"),
        ContextMessage(role="assistant", content="successful fallback"),
    )


def test_context_groups_legacy_fanout_without_random_request_ids() -> None:
    turns = [
        _turn(provider="gemini", prompt="same", response="g1", request_id=None),
        _turn(provider="openai", prompt="same", response="o1", request_id=None),
        _turn(provider="openai", prompt="same", response="o2", request_id=None),
    ]

    first = ConversationEntityClass.build_context(turns, "openai")
    second = ConversationEntityClass.build_context(turns, "openai")

    assert first == second
    assert first.messages == (
        ContextMessage(role="user", content="same"),
        ContextMessage(role="assistant", content="o1"),
        ContextMessage(role="user", content="same"),
        ContextMessage(role="assistant", content="o2"),
    )
