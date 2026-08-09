import asyncio
from types import SimpleNamespace

import pytest
from bson import ObjectId
from pydantic import ValidationError

from app.adapters.anthropic_adp import AnthropicAdapterClass
from app.adapters.gemini_adp import GeminiAdapterClass
from app.adapters.openai_adp import OpenAIAdapterClass
from app.core.errors import ConflictError, NotFoundError
from app.entities.conversation_ent import (
    ContextMessage,
    ConversationEntityClass,
    Project,
    Session,
    SessionContext,
    SessionTurn,
)
from app.entities.pipeline_llm import ImageOutput, LLMPipelineEntityClass, Source, StreamChunk
from app.routers.cortex.models import CortexModels
from app.routers.cortex.service import CortexServiceV1Class


class _ConversationFake:
    def __init__(self) -> None:
        self.saved: list[tuple[list[dict[str, str]], str | None]] = []

    async def create_turn(
        self,
        *,
        provider: str,
        session_id: str,
        request_id: ObjectId,
        prompt: str,
        provider_parent_response_id: str | None = None,
    ) -> SimpleNamespace:
        assert provider == "openai"
        assert session_id == "507f1f77bcf86cd799439011"
        assert isinstance(request_id, ObjectId)
        assert provider_parent_response_id is None
        return SimpleNamespace(
            id=ObjectId(),
            parts=[{"type": "user", "text": prompt}],
        )

    async def save_turn_parts_shielded(
        self,
        _turn_id: ObjectId,
        parts: list[dict[str, str]],
        provider_response_id: str | None = None,
    ) -> None:
        self.saved.append((parts.copy(), provider_response_id))


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
    response_id: str | None = None,
    parent_response_id: str | None = None,
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
        provider_response_id=response_id,
        provider_parent_response_id=parent_response_id,
        session_id=ObjectId(),
        created_at="2026-07-14T00:00:00Z",
        updated_at="2026-07-14T00:00:00Z",
    )


def test_listen_allows_session_to_be_created_on_first_turn() -> None:
    payload = CortexModels.V1.Input.Listen.model_validate(
        {
            "prompt": "hello",
            "providers": ["openai"],
            "projectId": "507f1f77bcf86cd799439011",
        }
    )

    assert payload.session_id is None
    assert payload.project_id == "507f1f77bcf86cd799439011"
    assert payload.gemini_mode == "generate_content"


@pytest.mark.parametrize(
    "payload",
    [
        {"prompt": "   ", "providers": ["openai"]},
        {"prompt": "hello", "providers": []},
        {"prompt": "hello", "providers": ["openai", "openai"]},
    ],
)
def test_listen_rejects_blank_prompt_and_invalid_provider_fanout(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        CortexModels.V1.Input.Listen.model_validate(payload)


@pytest.mark.asyncio
async def test_listen_creates_session_only_when_session_id_is_missing() -> None:
    session = _session(project_id=ObjectId())

    class _ConversationsFake:
        def __init__(self) -> None:
            self.created_with: str | None = None

        async def create_session(self, project_id: str | None) -> Session:
            self.created_with = project_id
            return session

        async def get_session(self, _session_id: str) -> Session:
            raise AssertionError("Existing-session lookup should not run")

    conversations = _ConversationsFake()
    service = object.__new__(CortexServiceV1Class)
    service.conversations = conversations  # type: ignore[assignment]
    payload = CortexModels.V1.Input.Listen.model_validate(
        {
            "prompt": "hello",
            "providers": ["openai"],
            "projectId": "507f1f77bcf86cd799439011",
        }
    )

    session_id = await service.resolve_listen_session(payload)

    assert session_id == str(session.id)
    assert conversations.created_with == "507f1f77bcf86cd799439011"


@pytest.mark.asyncio
async def test_listen_reuses_and_validates_existing_session() -> None:
    session = _session(project_id=None)

    class _ConversationsFake:
        def __init__(self) -> None:
            self.looked_up: str | None = None

        async def create_session(self, _project_id: str | None) -> Session:
            raise AssertionError("A second session must not be created")

        async def get_session(self, session_id: str) -> Session:
            self.looked_up = session_id
            return session

    conversations = _ConversationsFake()
    service = object.__new__(CortexServiceV1Class)
    service.conversations = conversations  # type: ignore[assignment]
    payload = CortexModels.V1.Input.Listen.model_validate(
        {
            "prompt": "hello",
            "providers": ["openai"],
            "sessionId": str(session.id),
        }
    )

    session_id = await service.resolve_listen_session(payload)

    assert session_id == str(session.id)
    assert conversations.looked_up == str(session.id)


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
            response_id="resp_first",
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
    assert context.previous_response_id is None


def test_context_uses_latest_target_provider_response_id() -> None:
    request_id = ObjectId()
    context = ConversationEntityClass.build_context(
        [
            _turn(
                provider="openai",
                prompt="first",
                response="answer",
                request_id=request_id,
                response_id="resp_latest",
            )
        ],
        "openai",
    )

    assert context.previous_response_id == "resp_latest"


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
                response_id="must_not_resume",
                done=False,
            ),
        ],
        "openai",
    )

    assert context.messages == (
        ContextMessage(role="user", content="first"),
        ContextMessage(role="assistant", content="successful fallback"),
    )
    assert context.previous_response_id is None


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


def test_context_rebuilds_after_concurrent_provider_continuation_branches() -> None:
    turns = [
        _turn(
            provider="openai",
            prompt="first branch",
            response="first answer",
            request_id=ObjectId(),
            response_id="resp_branch_one",
            parent_response_id="resp_parent",
        ),
        _turn(
            provider="openai",
            prompt="second branch",
            response="second answer",
            request_id=ObjectId(),
            response_id="resp_branch_two",
            parent_response_id="resp_parent",
        ),
    ]

    context = ConversationEntityClass.build_context(turns, "openai")

    assert context.previous_response_id is None
    assert len(context.messages) == 4


def test_sdk_history_builders_preserve_roles_and_append_current_prompt() -> None:
    messages = [("user", "old question"), ("assistant", "old answer")]

    assert OpenAIAdapterClass.build_text_input(messages, "new question") == [
        {"type": "message", "role": "user", "content": "old question"},
        {"type": "message", "role": "assistant", "content": "old answer"},
        {"type": "message", "role": "user", "content": "new question"},
    ]
    assert AnthropicAdapterClass.build_text_messages(messages, "new question") == [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "new question"},
    ]

    gemini = GeminiAdapterClass.build_generate_content_input(messages, "new question")
    assert [content.role for content in gemini] == ["user", "model", "user"]
    assert [content.parts[0].text for content in gemini if content.parts] == [
        "old question",
        "old answer",
        "new question",
    ]

    interactions = GeminiAdapterClass.build_interaction_input(messages, "new question")
    assert [step["type"] for step in interactions] == [
        "user_input",
        "model_output",
        "user_input",
    ]


def test_capture_part_compacts_text_and_preserves_structured_chunks() -> None:
    parts: list[dict[str, str]] = []

    for chunk in (
        StreamChunk(provider="openai", type="delta", text="hello "),
        StreamChunk(provider="openai", type="delta", text="world"),
        StreamChunk(
            provider="openai",
            type="info",
            info="response.output_item.done",
            tool_name="code_interpreter",
            tool_output="42",
            is_error=False,
        ),
        StreamChunk(
            provider="openai",
            type="source",
            sources=[Source(url="https://example.com", title="Example")],
        ),
        StreamChunk(
            provider="openai",
            type="image",
            info="response.output_item.done",
            tool_name="code_interpreter",
            images=[ImageOutput(url="data:image/png;base64,abc")],
        ),
        StreamChunk(provider="openai", type="done"),
    ):
        LLMPipelineEntityClass._capture_part(parts, chunk)

    assert parts == [
        {"type": "text", "text": "hello world"},
        {
            "type": "info",
            "info": "response.output_item.done",
            "toolName": "code_interpreter",
            "toolOutput": "42",
            "isError": "false",
        },
        {"type": "source", "url": "https://example.com", "title": "Example"},
        {
            "type": "image",
            "info": "response.output_item.done",
            "toolName": "code_interpreter",
            "url": "data:image/png;base64,abc",
        },
        {"type": "done"},
    ]


@pytest.mark.asyncio
async def test_closing_stream_persists_partial_turn() -> None:
    fake = _ConversationFake()
    pipeline = object.__new__(LLMPipelineEntityClass)
    pipeline._conversations = fake  # type: ignore[assignment]

    async def stream():
        yield StreamChunk(provider="openai", type="delta", text="partial")
        yield StreamChunk(provider="openai", type="done")

    persisted_stream = pipeline._persist_stream(
        stream(),
        provider="openai",
        session_id="507f1f77bcf86cd799439011",
        request_id=ObjectId(),
        prompt="question",
    )

    assert (await anext(persisted_stream)).text == "partial"
    await persisted_stream.aclose()

    assert fake.saved == [
        (
            [
                {"type": "user", "text": "question"},
                {"type": "text", "text": "partial"},
            ],
            None,
        )
    ]


@pytest.mark.asyncio
async def test_completed_stream_persists_provider_response_id() -> None:
    fake = _ConversationFake()
    pipeline = object.__new__(LLMPipelineEntityClass)
    pipeline._conversations = fake  # type: ignore[assignment]

    async def stream():
        yield StreamChunk(provider="openai", type="delta", text="answer")
        yield StreamChunk(
            provider="openai",
            type="done",
            provider_response_id="resp_saved",
        )

    chunks = [
        chunk
        async for chunk in pipeline._persist_stream(
            stream(),
            provider="openai",
            session_id="507f1f77bcf86cd799439011",
            request_id=ObjectId(),
            prompt="question",
        )
    ]

    assert [chunk.type for chunk in chunks] == ["delta", "done"]
    assert fake.saved == [
        (
            [
                {"type": "user", "text": "question"},
                {"type": "text", "text": "answer"},
                {"type": "done"},
            ],
            "resp_saved",
        )
    ]


@pytest.mark.asyncio
async def test_openai_chain_uses_native_compaction_and_captures_response_id() -> None:
    class _OpenAIFake:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] = {}

        async def stream_response(self, **kwargs):
            self.kwargs = kwargs

            async def events():
                yield SimpleNamespace(
                    type="response.completed",
                    response=SimpleNamespace(id="resp_new"),
                )

            return events()

        build_text_input = staticmethod(OpenAIAdapterClass.build_text_input)
        Tools = OpenAIAdapterClass.Tools

    openai = _OpenAIFake()
    pipeline = object.__new__(LLMPipelineEntityClass)
    pipeline._openai = openai  # type: ignore[assignment]
    context = SessionContext(previous_response_id="resp_previous", messages=())

    chunks = [chunk async for chunk in pipeline._openai_stream("next question", None, context)]

    assert openai.kwargs["input"] == "next question"
    assert openai.kwargs["previous_response_id"] == "resp_previous"
    assert openai.kwargs["store"] is True
    assert openai.kwargs["context_management"] == [
        {"type": "compaction", "compact_threshold": 150_000}
    ]
    assert chunks == [
        StreamChunk(
            provider="openai",
            type="done",
            provider_response_id="resp_new",
        )
    ]
    assert "provider_response_id" not in chunks[0].model_dump()


@pytest.mark.asyncio
async def test_openai_expired_chain_retries_with_complete_mongo_history() -> None:
    class _ExpiredResponseError(Exception):
        status_code = 404

        def __init__(self, message: str) -> None:
            super().__init__(message)
            self.body = {
                "error": {
                    "param": "previous_response_id",
                    "message": "Previous response not found",
                }
            }

    class _OpenAIFake:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def stream_response(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise _ExpiredResponseError("Previous response not found")

            async def events():
                yield SimpleNamespace(
                    type="response.completed",
                    response=SimpleNamespace(id="resp_rebuilt"),
                )

            return events()

        build_text_input = staticmethod(OpenAIAdapterClass.build_text_input)
        Tools = OpenAIAdapterClass.Tools

    openai = _OpenAIFake()
    pipeline = object.__new__(LLMPipelineEntityClass)
    pipeline._openai = openai  # type: ignore[assignment]
    context = SessionContext(
        previous_response_id="resp_expired",
        messages=(
            ContextMessage(role="user", content="old question"),
            ContextMessage(role="assistant", content="old answer"),
        ),
    )

    chunks = [chunk async for chunk in pipeline._openai_stream("new question", None, context)]

    assert len(openai.calls) == 2
    assert openai.calls[0]["input"] == "new question"
    assert openai.calls[0]["previous_response_id"] == "resp_expired"
    assert openai.calls[1]["input"] == [
        {"type": "message", "role": "user", "content": "old question"},
        {"type": "message", "role": "assistant", "content": "old answer"},
        {"type": "message", "role": "user", "content": "new question"},
    ]
    assert chunks[-1].provider_response_id == "resp_rebuilt"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_type", "response_field", "expected"),
    [
        ("response.failed", "error", "provider failed"),
        ("response.incomplete", "incomplete_details", "max_output_tokens"),
    ],
)
async def test_openai_noncompleted_terminal_events_are_errors_not_done(
    terminal_type: str,
    response_field: str,
    expected: str,
) -> None:
    class _OpenAIFake:
        async def stream_response(self, **_kwargs):
            response = SimpleNamespace(error=None, incomplete_details=None)
            if response_field == "error":
                response.error = SimpleNamespace(message=expected)
            else:
                response.incomplete_details = SimpleNamespace(reason=expected)

            async def events():
                yield SimpleNamespace(type=terminal_type, response=response)

            return events()

        build_text_input = staticmethod(OpenAIAdapterClass.build_text_input)
        Tools = OpenAIAdapterClass.Tools

    pipeline = object.__new__(LLMPipelineEntityClass)
    pipeline._openai = _OpenAIFake()  # type: ignore[assignment]

    chunks = [
        chunk
        async for chunk in pipeline._openai_stream(
            "question",
            None,
            SessionContext(messages=()),
        )
    ]

    assert [chunk.type for chunk in chunks] == ["error"]
    assert expected in chunks[0].text


@pytest.mark.asyncio
async def test_gemini_interactions_chain_falls_back_and_captures_new_id() -> None:
    class _GeminiFake:
        def __init__(self) -> None:
            self.calls: list[tuple[object, dict[str, object]]] = []

        async def stream_model_interaction_events(self, input, **kwargs):
            self.calls.append((input, kwargs))

            async def events():
                if len(self.calls) == 1:
                    yield SimpleNamespace(
                        event_type="error",
                        error=SimpleNamespace(
                            code="not_found",
                            message="previous_interaction_id expired",
                        ),
                    )
                else:
                    yield SimpleNamespace(
                        event_type="interaction.completed",
                        interaction=SimpleNamespace(id="interaction_rebuilt"),
                    )

            return events()

        build_interaction_input = staticmethod(GeminiAdapterClass.build_interaction_input)
        Tools = GeminiAdapterClass.Tools

    gemini = _GeminiFake()
    pipeline = object.__new__(LLMPipelineEntityClass)
    pipeline._gemini = gemini  # type: ignore[assignment]
    context = SessionContext(
        previous_response_id="interaction_expired",
        messages=(
            ContextMessage(role="user", content="old question"),
            ContextMessage(role="assistant", content="old answer"),
        ),
    )

    chunks = [
        chunk
        async for chunk in pipeline._gemini_stream_via_interactions(
            "new question",
            [],
            context,
        )
    ]

    assert len(gemini.calls) == 2
    first_input, first_kwargs = gemini.calls[0]
    assert first_input == "new question"
    assert first_kwargs["previous_interaction_id"] == "interaction_expired"
    assert first_kwargs["store"] is True
    second_input, second_kwargs = gemini.calls[1]
    assert [step["type"] for step in second_input] == [
        "user_input",
        "model_output",
        "user_input",
    ]
    assert second_kwargs["previous_interaction_id"] is None
    assert chunks[-1].provider_response_id == "interaction_rebuilt"


@pytest.mark.asyncio
async def test_gemini_failed_status_is_error_not_done() -> None:
    class _GeminiFake:
        async def stream_model_interaction_events(self, _input, **_kwargs):
            async def events():
                yield SimpleNamespace(
                    event_type="interaction.status_update",
                    status="failed",
                )

            return events()

        build_interaction_input = staticmethod(GeminiAdapterClass.build_interaction_input)
        Tools = GeminiAdapterClass.Tools

    pipeline = object.__new__(LLMPipelineEntityClass)
    pipeline._gemini = _GeminiFake()  # type: ignore[assignment]

    chunks = [
        chunk
        async for chunk in pipeline._gemini_stream_via_interactions(
            "question",
            [],
            SessionContext(messages=()),
        )
    ]

    assert [chunk.type for chunk in chunks] == ["error"]


def test_continuation_detection_does_not_retry_unrelated_provider_errors() -> None:
    error = SimpleNamespace(
        status_code=404,
        body={"error": {"message": "Model not found"}},
    )

    assert not LLMPipelineEntityClass._is_unusable_continuation_error(
        error,
        field_name="previous_response_id",
    )
