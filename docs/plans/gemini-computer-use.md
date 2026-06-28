# Plan: Gemini Computer Use

Status: **Deferred** — documented for later. Current focus is the pipeline side.
Last updated: 2026-06-28

## What this is

Gemini's **Computer Use** is a browser/UI automation capability: the model "sees" a
screenshot and returns UI actions (click, type, scroll) for *us* to execute, then we
feed back a fresh screenshot and loop. It is a `tools=[...]` entry passed to a capable
model — **not** a standalone product.

Unlike the other Gemini built-in tools (`google_search`, `code_execution`,
`url_context`), Google does **not** execute computer-use actions. We must supply the
sandboxed browser and run the action loop ourselves — the same model as OpenAI's
computer use.

## Key facts (sourced live, June 2026 — post knowledge cutoff)

- **Models:** `gemini-3.5-flash` (recommended, GA), `gemini-3-flash-preview`, legacy
  `gemini-2.5-computer-use-preview-10-2025`.
- **Status split:** the model is GA, but the **computer use capability is Preview**
  ("may contain errors and security vulnerabilities").
- **Tool config:** `{"type": "computer_use", "environment": "browser"}` (also
  `"mobile"` / `"desktop"`), plus `enable_prompt_injection_detection`.
- **Availability:** both AI Studio (api_key) and **Vertex AI** (our path). GA model on
  Vertex across Global/US/EU. No allowlist signup.
- **Prereqs:** `google-genai` >= 2.7.0, Playwright (`pip install playwright` +
  `playwright install chromium`), a sandboxed browser environment.
- **Reference impl:** https://github.com/google-gemini/computer-use-preview

### Sources
- https://ai.google.dev/gemini-api/docs/computer-use
- https://ai.google.dev/gemini-api/docs/interactions/computer-use
- https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash
- https://blog.google/innovation-and-ai/models-and-research/gemini-models/introducing-computer-use-gemini-3-5-flash/

## Why it doesn't fit the current pipeline

The current `LLMPipelineEntityClass` is "stream text deltas until done" — one direction,
provider → us. Computer use is a **two-way agentic loop**:

```
1. send:  prompt + screenshot + computer_use tool
2. model: returns function_call (action + coords normalized 0-999 + intent), then STOPS
3. us:    scale coords to viewport, execute action via Playwright
4. send:  fresh screenshot as function_result  → back to step 2
```

This requires holding browser state across turns, executing side effects, and looping
back into the model — fundamentally different control flow from text streaming. It must
be its own pipeline, not a flag on the existing one.

## Proposed shape (when we build it)

Follows the existing adapter -> entity rule. Nothing here is built yet.

### 1. Browser adapter — `app/adapters/browser_adp.py`
- Owns a Playwright `async_playwright` lifecycle (launch chromium, one context/page).
- `initialize()` / `close()` like other adapters; registered in `AdapterRegistry`.
- Methods the loop needs: `screenshot() -> bytes`, `click(x, y)`, `type(text)`,
  `scroll(...)`, `navigate(url)`, `viewport_size()` (for coord scaling 0-999 -> px).
- Runs headless in a sandbox; this is the security boundary — treat model actions as
  untrusted (see Security below).

### 2. Gemini adapter — extend `gemini_adp.py`
- Add a `computer_use_tool()` builder returning the `computer_use` tool config
  (mirrors the existing `builtin_tools()` pattern), with
  `enable_prompt_injection_detection=True`.
- The loop uses the non-streaming `generate_content` (turn must complete to read the
  function_call), so we likely add a non-streaming `generate(...)` method rather than
  reusing `stream(...)`.

### 3. Computer-use pipeline — `app/entities/pipeline_computer_use.py`
- New entity `ComputerUsePipelineEntityClass(gemini, browser)`.
- `run(task: str, *, max_steps: int)` async generator yielding normalized step events
  (action taken, intent, screenshot ref, done/error) so a future SSE endpoint can
  stream progress.
- Loop: screenshot -> Gemini -> parse function_call -> scale coords -> browser action
  -> repeat until model emits no action (done) or `max_steps` hit.
- Coordinate scaling: model returns 0-999 normalized; multiply by actual viewport
  width/height.
- Wire into `EntitiesRegistry` as `cls.ComputerUse`.

### 4. Config — `app/config.py`
- `computer_use_model: str = "gemini-3.5-flash"`
- `computer_use_max_steps: int = 25` (safety cap on the loop)
- Browser/Playwright knobs as needed (headless, viewport, timeout).

### 5. Dependencies
- `uv add playwright` + a build/runtime step for `playwright install chromium`
  (note: needs handling in the Dockerfile too).

## Security (must-haves before any real use)

- Run the browser in a **sandboxed/isolated environment** (container, no host network
  access beyond what the task needs).
- Keep `enable_prompt_injection_detection=True`.
- Consider an allow/block-list of domains the agent may navigate to.
- Human-in-the-loop confirmation for destructive actions (form submits, purchases) —
  Google's own guidance recommends this for a Preview capability.
- Hard `max_steps` cap and per-action timeouts to prevent runaway loops.

## Open questions / to verify when we pick this up

- Exact `function_call` action schema for `gemini-3.5-flash` on the Interactions API
  surface vs the legacy 2.5 model (action names, fields).
- Whether the `google-genai` SDK exposes a typed helper for the action parsing or we
  parse `parts[].function_call` manually.
- Vertex-specific: confirm `gemini-3.5-flash` computer use works under
  `location="global"` (our current setting) vs needing a regional endpoint.
- Dockerfile changes for Playwright/Chromium in the runtime image.

## Not doing now

Deferred until the pipeline-side work is finished. This doc is the starting point when
we return.
