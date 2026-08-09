# FE change spec: OpenAI tool payloads + `image` chunks on `/v1/cortex/listen`

Handoff doc for the frontend agent. The backend SSE stream from
`POST /v1/cortex/listen` now carries **real payloads for OpenAI built-in tool
activity** (code being written, execution results, search queries) and a **new
`image` chunk type** for tool-generated images (matplotlib charts, etc.).
Everything is additive — nothing that previously worked changes shape or disappears.
Full stream contract lives in `docs/frontend-integration.md`; this doc is only the
delta you need to map.

---

## 1. What changed, at a glance

| Before | After |
|---|---|
| OpenAI code-interpreter events arrived as bare labels (`info` = `"response.code_interpreter_call_code.delta"`, all payload fields `null`) | The same events now carry `toolName` / `toolInput` / `toolOutput` / `toolLanguage` / `isError`, exactly like the Gemini `code_execution` chunks you already render |
| Code-interpreter execution output was never sent | A new `info` chunk with `info: "response.output_item.done"` carries `toolOutput` (stdout/logs) and `isError` |
| Web-search queries were never sent | A new `info` chunk with `info: "response.output_item.done"` and `toolName: "web_search"` carries the actual query in `toolInput` |
| Tool-generated images were never sent | New chunk type `type: "image"` with an `images` array of base64 data URIs |

## 2. Schema changes on `StreamChunk`

Two additions; every other field is unchanged:

```jsonc
{
  "provider": "openai" | "gemini" | "anthropic",
  "type": "delta" | "info" | "source" | "image" | "done" | "error",  // "image" is NEW
  "text": "",
  "info": null,
  "toolName": null,
  "toolInput": null,
  "toolOutput": null,
  "toolLanguage": null,
  "isError": null,
  "sources": [],
  "images": [ { "url": "data:image/png;base64,..." } ],  // NEW field, [] unless type === "image"
  "mapsWidgetContextToken": null
}
```

The SSE `event:` line always equals `data.type`, so a frame can now also be
`event: image`. If your parser switches on an exhaustive union of event names,
add `"image"` — unknown-event fallthrough that drops the frame would silently
lose charts.

## 3. New OpenAI chunk sequences to handle

### 3a. Code interpreter — code streams in, then the result

Real captured sequence (one chunk per line, irrelevant fields omitted):

```jsonc
// lifecycle label, unchanged from before (no payload)
{ "provider":"openai", "type":"info", "info":"response.code_interpreter_call.in_progress" }

// NEW: the code the model is writing, streamed as fragments — append in order
{ "provider":"openai", "type":"info", "info":"response.code_interpreter_call_code.delta",
  "toolName":"code_interpreter", "toolInput":"import", "toolLanguage":"python" }
{ "provider":"openai", "type":"info", "info":"response.code_interpreter_call_code.delta",
  "toolName":"code_interpreter", "toolInput":" sym", "toolLanguage":"python" }
{ "provider":"openai", "type":"info", "info":"response.code_interpreter_call_code.delta",
  "toolName":"code_interpreter", "toolInput":"py as sp\n", "toolLanguage":"python" }
// ...more fragments...

// lifecycle labels, unchanged (the full code already arrived via the deltas above)
{ "provider":"openai", "type":"info", "info":"response.code_interpreter_call_code.done" }
{ "provider":"openai", "type":"info", "info":"response.code_interpreter_call.interpreting" }
{ "provider":"openai", "type":"info", "info":"response.code_interpreter_call.completed" }

// NEW: execution finished — stdout/logs + error flag
{ "provider":"openai", "type":"info", "info":"response.output_item.done",
  "toolName":"code_interpreter", "toolOutput":"(5117, 229)", "isError":false }
```

**How to render:** identical to your existing Gemini path —

- `toolName === "code_interpreter"` with non-null `toolInput` → append `toolInput`
  fragments to a live code block (Gemini's `code_execution_call` gives the code in
  one chunk; OpenAI streams it in fragments — accumulate per provider).
- `toolName === "code_interpreter"` with non-null `toolOutput` → show as the
  execution result under the code block; style as an error when `isError === true`.
- `toolOutput` may be `""` when the run printed nothing — treat empty as
  "finished, no output", not as missing.

### 3b. Code interpreter — chart/image output (NEW chunk type)

When the executed code produces images, an `image` chunk follows the
`toolOutput` chunk above:

```jsonc
{ "provider":"openai", "type":"image", "info":"response.output_item.done",
  "toolName":"code_interpreter",
  "images":[ { "url":"data:image/png;base64,iVBORw0KGgo..." } ] }
```

- `images[].url` is a **self-contained `data:image/png;base64,...` URI** — set it
  straight into `<img src>`; no fetch, no auth, no proxy.
- The array can hold multiple images (one per figure the code showed).
- These are typically large payloads (hundreds of KB in one SSE frame) — make
  sure your frame parser buffers until the blank-line delimiter rather than
  assuming one `read()` = one frame. (If you already parse per spec, nothing to do.)
- Not a citation — do **not** route these into the sources UI.

### 3c. Web search — the actual query (NEW payload)

The existing lifecycle labels (`response.web_search_call.in_progress` /
`.searching` / `.completed`) still arrive unchanged. After them, a payload chunk:

```jsonc
{ "provider":"openai", "type":"info", "info":"response.output_item.done",
  "toolName":"web_search", "toolInput":"sum of first 50 prime numbers", "isError":false }
```

`toolInput` is the query the model actually searched — or, for page visits, the
URL opened / `find 'pattern' in <url>`. Render it in the activity timeline
(e.g. `Searched: "sum of first 50 prime numbers"`) instead of the bare event name.
Cited sources still arrive via `source` chunks exactly as before.

## 4. Discriminating the new `info` chunks

`info: "response.output_item.done"` is shared by both tools — **discriminate on
`toolName`, not on the `info` label**:

```ts
if (chunk.type === "info" && chunk.toolName === "code_interpreter") {
  if (chunk.toolInput != null)  appendCode(chunk.provider, chunk.toolInput, chunk.toolLanguage);
  if (chunk.toolOutput != null) showResult(chunk.provider, chunk.toolOutput, chunk.isError);
} else if (chunk.type === "info" && chunk.toolName === "web_search") {
  if (chunk.toolInput) showSearchQuery(chunk.provider, chunk.toolInput);
} else if (chunk.type === "info") {
  showActivityBadge(chunk.provider, chunk.info);   // existing behavior, unchanged
} else if (chunk.type === "image") {
  chunk.images.forEach(img => showImage(chunk.provider, img.url, chunk.toolName));
}
```

This branch structure also keeps you aligned with the Gemini chunks
(`toolName: "code_execution"`), which use the same fields.

## 5. Backwards compatibility / regression notes

- All previously-emitted chunks still arrive with the same shape and order;
  the new payload chunks and `image` chunks are additions in between.
- `images` is present (as `[]`) on **every** chunk now, like `sources` — don't
  assume its absence.
- A code-interpreter turn with no printed output still emits the
  `response.output_item.done` chunk with `toolOutput: ""` — handle empty string.
- Gemini is untouched: same events, same fields as before.
