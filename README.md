# Cortex

Cortex is an experimentation platform for building a larger personal intelligence system. It is not one assistant, model, or pipeline. It is the shared brain under a family of projects: multiple interaction pipelines, agentic orchestration layers, provider adapters, execution harnesses, tools, memories, and user-facing clients that can evolve independently while speaking a common runtime contract.

The goal is to make capabilities composable. Voice, screen understanding, coding agents, research workers, desktop control, messaging adapters, and future projects should not each grow a separate brain. They should enter Cortex through adapters, use shared session and execution primitives, and return normalized events to whichever client invoked them.

## Mental model

```text
clients and surfaces
macOS companion · web app · Discord · CLI · future projects
                         │
                         ▼
interaction pipelines
listen · transcribe · screen guide · delegate · research · code
                         │
                         ▼
Cortex orchestration
sessions · context · routing · tools · workers · approvals · events
                         │
                         ▼
execution harnesses
LLM providers · Agent SDK · Codex · OpenCode · MCP · sandboxes
                         │
                         ▼
infrastructure
MongoDB · Redis · object storage · telemetry
```

A pipeline owns a user-visible interaction loop. A harness owns how work is executed. An adapter owns an external provider or transport. Cortex composes them without exposing those implementation choices as separate assistants.

## What exists today

The backend is a Python 3.12+ FastAPI service with:

- streamed multi-provider model responses through `POST /v1/cortex/listen`;
- Anthropic, OpenAI, and Gemini adapters;
- persistent Projects, Sessions, and SessionTurns;
- lazy session creation and continuation through `X-Session-ID`;
- normalized Server-Sent Events for text, sources, images, completion, and errors;
- realtime Cartesia speech-to-text through `WS /v1/cortex/transcribe`;
- Redis, MongoDB, authentication, and an emerging agent-SDK execution pipeline.

The repository is intentionally an experimentation platform. A capability being present does not mean its product contract is final.

## Planned pipeline: voice, screen, and spatial guidance

This pipeline gives Cortex a macOS interaction similar in spirit to modern screen-aware voice assistants: press a hotkey, speak about what is visible, optionally mark a specific region, and receive spoken or on-screen guidance. A separate delegation path can turn the same request into durable agent work.

### Interaction modes

| Mode | Purpose | Terminal result |
| --- | --- | --- |
| Dictation | Convert speech into text in the focused app | Inserted text |
| Guide | Answer using voice and current-screen context | Speech, text, and optional overlay actions |
| Spatial guide | Answer about a user-marked screen region | Region-grounded answer and optional pointing |
| Delegate | Perform a task using tools or workers | Durable run with streamed progress and result |

### End-to-end flow

```text
global hotkey down
  → capture microphone + active screen + active app metadata
  → show a transparent annotation overlay
  → stream audio to /v1/cortex/transcribe
  → collect optional pointer strokes while the hotkey is held
global hotkey up
  → finalize transcript and annotation
  → derive stroke bounds, marked crop, and intersecting accessibility elements
  → route to Dictation, Guide, Spatial Guide, or Delegate
  → stream model/worker events
  → play speech, render pointing overlays, insert text, or show agent progress
```

### Spatial context

Spatial context binds words such as “this,” “here,” or “that button” to a precise screen region.

The native client freezes or captures the current screen and places a transparent full-screen drawing window above it. While the interaction hotkey is held, mouse-down and pointer-move events are stored as timestamped screen coordinates and rendered as a stroke. Mouse-up finishes one stroke; releasing the hotkey commits the complete request.

The client then sends a compact context package:

```json
{
  "display": {"id": "main", "scale": 2.0},
  "screenshot": "attachment-reference",
  "transcript": "What does this button do?",
  "strokes": [[{"x": 910, "y": 54}, {"x": 1008, "y": 96}]],
  "region": {"x": 900, "y": 44, "width": 118, "height": 62},
  "accessibility_elements": ["ax:export-button"]
}
```

The model receives the original screenshot, an annotated image or mask, the marked crop, normalized coordinates, and the relevant accessibility elements. This removes most ambiguity without requiring the model to infer the target from language alone.

### Accessibility tree

macOS Accessibility APIs expose a structured tree of visible interface elements. A screenshot says what the screen looks like; the accessibility tree says that a region is an enabled button named `Export`, gives its bounds, and may expose an action for pressing it.

```json
{
  "id": "ax:export-button",
  "role": "button",
  "label": "Export",
  "frame": {"x": 920, "y": 60, "width": 80, "height": 32},
  "enabled": true,
  "actions": ["press"]
}
```

Cortex should use the screenshot for visual meaning, annotation coordinates for user intent, and accessibility data for reliable identity and action. Raw pixel clicks remain a fallback for inaccessible interfaces.

### Assistant pointing and drawing

The model must not emit prose that the client parses heuristically. It returns typed overlay actions:

```json
{
  "type": "overlay.point",
  "target": {"accessibility_id": "ax:export-button"},
  "label": "Export your project here"
}
```

The native client resolves the target to current screen coordinates and renders a temporary click-through cursor, circle, arrow, or caption. Targets should be revalidated immediately before rendering because windows can move between capture and response.

### Delegation and sub-workers

Delegation is not merely parallel model calls. It creates a durable run owned by the current Cortex session. The orchestrator decomposes work, runs independent child tasks concurrently when useful, and merges their structured outputs.

Workers should prefer direct tools and integrations—filesystem, Gmail, Notion, browser APIs, MCP—over visible computer control. Code and shell work runs inside explicitly scoped sandboxes or worktrees. Computer use is the last-mile fallback for UI-only operations. Sending, deleting, purchasing, changing access, or other consequential actions must pause for approval.

The client sees one Cortex, not a list of hidden engines. Provider and harness selection stays behind the orchestration boundary.

## Why this is achievable

The system divides cleanly along platform boundaries:

- **Native macOS client (Swift/AppKit):** global shortcuts, microphone capture, screen capture, Accessibility APIs, focused-app text insertion, coordinate conversion, and transparent overlays.
- **Cortex backend (Python/FastAPI):** STT session control, multimodal request assembly, mode routing, model/provider adapters, durable conversations, agent orchestration, approvals, tool execution, and normalized streaming events.
- **Execution harnesses:** isolated child processes, Codex/OpenCode/Agent SDK adapters, MCP integrations, bounded parallelism, cancellation, and sandbox policy.

Much of the backend foundation already exists: realtime STT, provider adapters, streamed events, and durable sessions. The work is therefore incremental rather than a rewrite.

### Recommended delivery order

1. Ship dictation: hotkey → audio → existing transcription WebSocket → focused-app insertion.
2. Ship screen-aware Guide with one screenshot and active-app metadata.
3. Add the annotation overlay and region-grounded multimodal request.
4. Add accessibility-tree harvesting and element-to-region matching.
5. Add typed assistant pointing overlays without click execution.
6. Add safe single-worker delegation with durable run events and cancellation.
7. Add structured integrations, approvals, sandboxed execution, and bounded child-worker concurrency.
8. Add computer-use fallback only after coordinate, focus, permission, and safety tests pass.

The highest-risk engineering areas are multi-display coordinate normalization, Retina scaling, focus changes, incomplete accessibility trees, permission onboarding, capture-to-response latency, stale element targets, and cancellation/approval behavior. These should be explicit acceptance-test categories, not late polish.

## Development

Install dependencies and run the API:

```bash
uv sync
uv run uvicorn app.main:app --reload
```

Run validation:

```bash
uv run pytest
uv run ruff check app tests
uv run pyright
```

The service exposes `GET /health` for a basic runtime check. Configuration is loaded from the environment through `app/config.py`; use `.env.example` as the starting contract and never commit credentials.

## Research

- [Current HeyClicky product snapshot](docs/research/heyclicky-current-product-2026-08-01.md)
- [Local execution adapters](docs/research/cortex-local-execution-adapters.html)
- [HeyClicky screen-perception teardown](docs/research/heyclicky-screen-perception.html)

Research artifacts are point-in-time evidence. Product claims, provider capabilities, private implementation inferences, and Cortex recommendations must remain clearly separated.
