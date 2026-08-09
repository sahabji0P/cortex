"""Offline tests for the Agent SDK pipeline's message → StreamChunk mapping.

No API calls and no spawned harness: SDK message dataclasses are constructed
directly and fed through AgentSDKPipelineEntityClass._to_chunks.
"""

import json

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from app.entities.pipeline_agent_sdk import AgentSDKPipelineEntityClass


def _map(message: object) -> list:
    return list(AgentSDKPipelineEntityClass()._to_chunks(message))


def test_system_init_maps_to_info_with_session_and_tools() -> None:
    chunks = _map(
        SystemMessage(
            subtype="init",
            data={"session_id": "s-1", "model": "claude-sonnet-5", "tools": ["Read", "Bash"]},
        )
    )
    assert len(chunks) == 1
    (chunk,) = chunks
    assert chunk.type == "info"
    assert chunk.info == "harness.init"
    payload = json.loads(chunk.tool_output or "{}")
    assert payload["session_id"] == "s-1"
    assert payload["tools"] == ["Read", "Bash"]


def test_assistant_text_and_tool_use_map_to_delta_and_info() -> None:
    chunks = _map(
        AssistantMessage(
            model="claude-sonnet-5",
            content=[
                TextBlock(text="let me read that file"),
                ToolUseBlock(id="t-1", name="Read", input={"file_path": "a.py"}),
            ],
        )
    )
    assert [chunk.type for chunk in chunks] == ["delta", "info"]
    assert chunks[0].text == "let me read that file"
    assert chunks[1].info == "tool_use"
    assert chunks[1].tool_name == "Read"
    assert json.loads(chunks[1].tool_input or "{}") == {"file_path": "a.py"}


def test_tool_result_maps_to_info_with_error_flag() -> None:
    chunks = _map(
        UserMessage(
            content=[ToolResultBlock(tool_use_id="t-1", content="boom", is_error=True)],
        )
    )
    (chunk,) = chunks
    assert chunk.type == "info"
    assert chunk.info == "tool_result"
    assert chunk.tool_output == "boom"
    assert chunk.is_error is True


def test_success_result_maps_to_done_with_resumable_session_id() -> None:
    chunks = _map(
        ResultMessage(
            subtype="success",
            duration_ms=100,
            duration_api_ms=80,
            is_error=False,
            num_turns=3,
            session_id="s-9",
            total_cost_usd=0.01,
        )
    )
    (chunk,) = chunks
    assert chunk.type == "done"
    assert chunk.provider_response_id == "s-9"
    assert "turns=3" in (chunk.info or "")


def test_error_result_yields_error_chunk_before_done() -> None:
    chunks = _map(
        ResultMessage(
            subtype="error_max_turns",
            duration_ms=100,
            duration_api_ms=80,
            is_error=True,
            num_turns=10,
            session_id="s-10",
            result="hit the turn cap",
        )
    )
    assert [chunk.type for chunk in chunks] == ["error", "done"]
    assert "error_max_turns" in chunks[0].text
