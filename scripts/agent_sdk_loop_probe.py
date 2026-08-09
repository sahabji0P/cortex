"""Watch the Claude Agent SDK's agent loop run, message by message.

The SDK's loop source is closed (it lives inside the bundled Claude Code binary),
but its behavior is fully observable: every message of the loop streams to us.
This probe prints each one as it arrives — the harness init (with the full tool
list the model was given), every assistant text/thinking/tool_use block, every
tool_result coming back, a PreToolUse hook firing between the two, and the final
result with cost/turn stats. Afterwards it points at the session's JSONL event
log on disk, where the whole loop is persisted line by line.

Run from anywhere (cortex's venv via uv):
    uv run python scripts/agent_sdk_loop_probe.py "What files are in the app/entities directory?"
    uv run python scripts/agent_sdk_loop_probe.py "Fix the typo in README.md" \
        --tools Read,Edit,Glob --permission-mode acceptEdits --max-turns 5

Auth mirrors cortex: Vertex via GCP_PROJECT_ID/GCP_CREDENTIALS_PATH from .env,
else ANTHROPIC_API_KEY from the ambient environment.
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
os.chdir(REPO_ROOT)  # app.config resolves .env relative to cwd
sys.path.insert(0, str(REPO_ROOT))

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    HookContext,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from app.config import settings  # noqa: E402
from app.entities.pipeline_agent_sdk import AgentSDKPipelineEntityClass  # noqa: E402

_TRUNCATE_AT = 400


def _trunc(text: str, limit: int = _TRUNCATE_AT) -> str:
    """Keep console output readable; the session JSONL has the full payloads."""
    collapsed = text.replace("\n", " ")
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[:limit]}… (+{len(collapsed) - limit} chars)"


def _apply_gcp_credentials() -> None:
    """Mirror main.py's lifespan hook so Vertex auth works outside uvicorn."""
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or not settings.gcp_credentials_path:
        return
    path = Path(settings.gcp_credentials_path).expanduser()
    if path.is_file():
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(path)


async def _pre_tool_hook(input_data: dict, tool_use_id: str | None, context: HookContext) -> dict:
    """PreToolUse hook: fires after the model asks for a tool, before it executes.

    This is the seam where you'd validate, rewrite, or block tool calls. Here we
    just prove the interception point exists by printing what passed through.
    """
    tool_name = input_data.get("tool_name", "?")
    tool_input = json.dumps(input_data.get("tool_input", {}), default=str)
    print(f"  [HOOK] PreToolUse intercepted: {tool_name} {_trunc(tool_input, 200)}")
    return {}


def _print_message(message: object) -> None:
    """Render one raw SDK message; this is the agent loop made visible."""
    if isinstance(message, SystemMessage):
        if message.subtype == "init":
            data = message.data
            print("[INIT] harness spawned, session ready")
            print(f"       session_id : {data.get('session_id')}")
            print(f"       model      : {data.get('model')}")
            print(f"       cwd        : {data.get('cwd')}")
            print(f"       tools      : {', '.join(str(t) for t in data.get('tools', []))}")
        else:
            print(f"[SYSTEM] {message.subtype}")
        return

    if isinstance(message, AssistantMessage):
        where = " (subagent)" if message.parent_tool_use_id else ""
        for block in message.content:
            if isinstance(block, TextBlock):
                print(f"[TEXT]{where} {_trunc(block.text)}")
            elif isinstance(block, ThinkingBlock):
                print(f"[THINKING]{where} {_trunc(block.thinking)}")
            elif isinstance(block, ToolUseBlock):
                tool_input = json.dumps(block.input, default=str)
                print(f"[TOOL_USE]{where} {block.name} {_trunc(tool_input, 200)}")
        return

    if isinstance(message, UserMessage):
        content = message.content if isinstance(message.content, list) else []
        for block in content:
            if isinstance(block, ToolResultBlock):
                body = (
                    block.content
                    if isinstance(block.content, str)
                    else json.dumps(block.content, default=str)
                )
                tag = "TOOL_RESULT_ERROR" if block.is_error else "TOOL_RESULT"
                print(f"[{tag}] {_trunc(body or '')}")
        return

    if isinstance(message, ResultMessage):
        cost = f"${message.total_cost_usd:.4f}" if message.total_cost_usd else "n/a"
        print(f"[RESULT] {message.subtype} (is_error={message.is_error})")
        print(
            f"         turns={message.num_turns} cost={cost} "
            f"duration={message.duration_ms}ms stop={message.stop_reason}"
        )
        if message.result:
            print(f"         result: {_trunc(message.result)}")


async def main() -> tuple[str, str | None]:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("prompt", help="what to ask the agent")
    parser.add_argument(
        "--tools",
        default="Read,Glob,Grep",
        help="comma-separated tool allow-list (default: read-only Read,Glob,Grep)",
    )
    parser.add_argument("--permission-mode", default="default")
    parser.add_argument("--max-turns", type=int, default=None)
    parser.add_argument(
        "--model",
        default=None,
        help="model id for the harness (e.g. claude-opus-4-8 on Vertex); "
        "default: whatever the deployment provides",
    )
    parser.add_argument("--cwd", default=str(REPO_ROOT), help="agent working directory")
    args = parser.parse_args()

    _apply_gcp_credentials()

    options = ClaudeAgentOptions(
        allowed_tools=[tool.strip() for tool in args.tools.split(",")],
        permission_mode=args.permission_mode,
        cwd=args.cwd,
        model=args.model,
        max_turns=args.max_turns,
        setting_sources=[],  # ignore ~/.claude and project settings: deterministic runs
        env=AgentSDKPipelineEntityClass._vertex_env(),
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[_pre_tool_hook])]},
    )

    session_id: str | None = None
    try:
        async for message in query(prompt=args.prompt, options=options):
            if isinstance(message, SystemMessage) and message.subtype == "init":
                session_id = str(message.data.get("session_id"))
            _print_message(message)
    except Exception as exc:
        # A failed run raises after yielding its error result; print and exit cleanly.
        print(f"[ERROR] {exc}")

    return args.cwd, session_id


def _print_session_log(cwd: str, session_id: str | None) -> None:
    """Point at the session's on-disk JSONL event log (sync; run after asyncio.run)."""
    slug = str(Path(cwd).resolve()).replace("/", "-")
    session_file = Path.home() / ".claude" / "projects" / slug / f"{session_id}.jsonl"
    if session_file.is_file():
        lines = session_file.read_text(encoding="utf-8").splitlines()
        print(f"\n[SESSION LOG] {session_file} ({len(lines)} events)")
        print("              every loop event is one JSON line — `head -3` it or pipe to jq")


if __name__ == "__main__":
    cwd, session_id = asyncio.run(main())
    _print_session_log(cwd, session_id)
