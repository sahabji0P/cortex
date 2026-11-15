import json
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    Message,
    PermissionMode,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings

if TYPE_CHECKING:
    from claude_agent_sdk.types import SystemPromptPreset


class StreamChunk(BaseModel):
    """One normalized event of an agent run.

    - `delta` : output text (in `text`)
    - `info`  : a tool call, tool result, thinking block, or harness lifecycle event
    - `done`  : the run finished (stats in `info`)
    - `error` : the run failed (message in `text`)
    """

    provider: Literal["anthropic"]
    type: Literal["delta", "info", "done", "error"]
    text: str = ""
    info: str | None = None
    tool_name: str | None = Field(default=None, serialization_alias="toolName")
    tool_input: str | None = Field(default=None, serialization_alias="toolInput")
    tool_output: str | None = Field(default=None, serialization_alias="toolOutput")
    is_error: bool | None = Field(default=None, serialization_alias="isError")
    provider_response_id: str | None = Field(default=None, exclude=True)

    model_config = ConfigDict(serialize_by_alias=True)


class AgentSDKPipelineEntityClass:
    """Runs a prompt through the Claude Agent SDK and yields a unified stream.

    This pipeline embeds the Claude Code harness — a local
    agent loop that calls the Messages API, executes file/shell tools on this machine,
    feeds results back to the model, and repeats until the model stops. Every step of
    that loop streams here as normalized chunks: text as `delta`, tool calls/results
    and harness lifecycle as `info`, and final run stats on `done` (whose
    provider_response_id carries the resumable SDK session id).
    """

    async def run(
        self,
        prompt: str,
        *,
        allowed_tools: list[str] | None = None,
        permission_mode: PermissionMode = "default",
        cwd: str | None = None,
        model: str | None = None,
        max_turns: int | None = None,
        max_budget_usd: float | None = None,
        system_prompt_append: str | None = None,
        resume_session_id: str | None = None,
        load_claude_settings: bool = False,
    ) -> AsyncIterator[StreamChunk]:
        """Run one agent loop and stream every observable step of it."""
        system_prompt: SystemPromptPreset | None = None
        if system_prompt_append is not None:
            system_prompt = {
                "type": "preset",
                "preset": "claude_code",
                "append": system_prompt_append,
            }
        options = ClaudeAgentOptions(
            # Default to a read-only agent: it can analyze but not modify anything.
            allowed_tools=(
                allowed_tools if allowed_tools is not None else ["Read", "Glob", "Grep"]
            ),
            permission_mode=permission_mode,
            cwd=cwd,
            model=model,
            max_turns=max_turns,
            max_budget_usd=max_budget_usd,
            system_prompt=system_prompt,
            resume=resume_session_id,
            # Empty list = don't load ~/.claude or project .claude settings, so runs are
            # deterministic regardless of local machine state. None = load all sources.
            setting_sources=[] if not load_claude_settings else None,
            env=self._vertex_env(),
        )
        try:
            async for message in query(prompt=prompt, options=options):
                for chunk in self._to_chunks(message):
                    yield chunk
        except Exception as exc:  # surface harness errors as a stream event, don't crash the run
            yield StreamChunk(provider="anthropic", type="error", text=str(exc))

    def _to_chunks(self, message: Message) -> Iterator[StreamChunk]:
        """Map one SDK message to normalized stream chunks.

        The mapping makes the agent loop legible: AssistantMessage carries the model's
        output (text/thinking/tool_use), UserMessage carries tool_results coming back,
        SystemMessage announces harness init (session id, model, full tool list), and
        ResultMessage closes the run with cost/turn stats.
        """
        if isinstance(message, SystemMessage):
            if message.subtype == "init":
                data = message.data
                yield StreamChunk(
                    provider="anthropic",
                    type="info",
                    info="harness.init",
                    tool_output=json.dumps(
                        {
                            "session_id": data.get("session_id"),
                            "model": data.get("model"),
                            "cwd": data.get("cwd"),
                            "tools": data.get("tools"),
                        },
                        default=str,
                    ),
                )
            return

        if isinstance(message, AssistantMessage):
            # parent_tool_use_id is set when this message is from a subagent.
            for block in message.content:
                if isinstance(block, TextBlock):
                    yield StreamChunk(provider="anthropic", type="delta", text=block.text)
                elif isinstance(block, ThinkingBlock):
                    yield StreamChunk(
                        provider="anthropic",
                        type="info",
                        info="thinking",
                        tool_input=block.thinking,
                    )
                elif isinstance(block, ToolUseBlock):
                    yield StreamChunk(
                        provider="anthropic",
                        type="info",
                        info="tool_use",
                        tool_name=block.name,
                        tool_input=json.dumps(block.input, default=str),
                    )
            return

        if isinstance(message, UserMessage):
            content = message.content
            blocks = content if isinstance(content, list) else []
            for block in blocks:
                if isinstance(block, ToolResultBlock):
                    yield StreamChunk(
                        provider="anthropic",
                        type="info",
                        info="tool_result",
                        tool_output=self._result_text(block),
                        is_error=bool(block.is_error),
                    )
            return

        if isinstance(message, ResultMessage):
            cost = f"${message.total_cost_usd:.4f}" if message.total_cost_usd else "n/a"
            if message.is_error:
                yield StreamChunk(
                    provider="anthropic",
                    type="error",
                    text=f"Agent run failed ({message.subtype}): {message.result}",
                )
            yield StreamChunk(
                provider="anthropic",
                type="done",
                info=(
                    f"{message.subtype}: turns={message.num_turns} "
                    f"cost={cost} duration={message.duration_ms}ms"
                ),
                provider_response_id=message.session_id,
            )

    @staticmethod
    def _result_text(block: ToolResultBlock) -> str:
        """Flatten a tool_result's content (str or content-block list) to text."""
        content = block.content
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        return json.dumps(content, default=str)

    @staticmethod
    def _vertex_env() -> dict[str, str]:
        """Route the spawned harness at Vertex AI.

        The SDK spawns a Claude Code subprocess; these vars are injected into its
        environment. When no GCP project is configured we inject nothing and the
        harness falls back to ANTHROPIC_API_KEY from the ambient environment.
        """
        env: dict[str, str] = {}
        if not settings.gcp_project_id:
            return env
        env["CLAUDE_CODE_USE_VERTEX"] = "1"
        env["ANTHROPIC_VERTEX_PROJECT_ID"] = settings.gcp_project_id
        env["CLOUD_ML_REGION"] = settings.vertex_region
        credentials = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or (
            str(Path(settings.gcp_credentials_path).expanduser())
            if settings.gcp_credentials_path
            else ""
        )
        if credentials:
            env["GOOGLE_APPLICATION_CREDENTIALS"] = credentials
        return env
