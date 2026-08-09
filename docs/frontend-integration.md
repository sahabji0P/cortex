# Cortex — Frontend Integration Guide

How a frontend (or an agent driving the UI) should call the Cortex API: auth, error
handling, and consuming the `cortex/listen` Server-Sent Events stream.

Last updated: 2026-07-19

---

## Conventions

- **Base URL:** `http://localhost:8000` in local dev. All app routes are versioned
  under `/v1` (the `/health` check is unversioned).
- **Request bodies:** JSON. Send `Content-Type: application/json`.
- **Auth:** Bearer token (JWT) in the `Authorization` header on protected routes:
  `Authorization: Bearer <access_token>`.

### Error shapes (two kinds — handle both)

1. **Application errors** (`401/403/404/409`, and any handled `AppError`):
   ```json
   { "code": "unauthorized", "message": "Invalid token" }
   ```
   `code` is a stable string (`unauthorized`, `forbidden`, `not_found`, `conflict`,
   `internal_error`); `message` is human-readable.

2. **Request validation errors** (`422`, FastAPI default — bad/missing fields):
   ```json
   { "detail": [ { "loc": ["body", "email"], "msg": "...", "type": "..." } ] }
   ```
   Different shape — check for `detail` being an array.

> ⚠️ The `cortex/listen` stream has a **third** error channel: once the HTTP response
> has started (status 200), provider failures arrive as in-stream `event: error`
> frames, **not** as an HTTP error. See [Errors in the stream](#errors-in-the-stream).

---

## Auth

### Register — `POST /v1/users/register`
```json
// request
{ "email": "you@example.com", "password": "secret" }
// 201 response
{ "id": "665...", "email": "you@example.com", "is_active": true, "created_at": "2026-06-28T..." }
```
`409 { "code": "conflict", "message": "Email already registered" }` if the email exists.

### Login — `POST /v1/users/login`
```json
// request
{ "email": "you@example.com", "password": "secret" }
// 200 response
{ "access_token": "<jwt>", "token_type": "bearer" }
```
`401 { "code": "unauthorized", "message": "Invalid email or password" }` on bad creds.

Store `access_token` and send it as `Authorization: Bearer <access_token>` on protected
calls.

### Current user — `GET /v1/users/me`  (requires Bearer)
```json
// 200 response
{ "id": "665...", "email": "you@example.com", "is_active": true, "created_at": "..." }
```
`401` if the token is missing or invalid.

---

## Streaming: `POST /v1/cortex/listen`

Streams model output as **Server-Sent Events (SSE)**. Route one prompt to one or more
providers; their output streams back as it's generated.

### Request
```json
{
  "prompt": "Explain CRDTs in two sentences.",
  "providers": ["gemini"],         // openai, gemini, or anthropic — unique; order matters
  "sessionId": "6875e7c7289c64c5c539e021", // omit for the first turn
  "geminiMode": "generate_content",
  "geminiTools": [],
  "openaiThinking": null
}
```
- `providers` runs **sequentially in the given order** (e.g. `["gemini","openai"]`
  streams all of Gemini, then all of OpenAI — not interleaved). It must contain at
  least one provider and cannot contain duplicates.
- For an existing conversation, `sessionId` must identify an existing session. An
  invalid or unknown value returns `404` before the SSE response starts.
- For a new conversation, omit `sessionId`. The backend creates the session when this
  first turn is submitted. Include optional `projectId` to place it in a project.
  Read the resulting ID from the `X-Session-ID` response header and reuse it on every
  later turn. Do not create a database session when the user merely clicks **New
  session**.
- Each provider run is persisted as a `SessionTurn` with an ObjectId `session_id`,
  provider, the user prompt plus compact ordered output parts, and server-managed
  timestamps. Prior turns are replayed as context on the next request in that session.
  All providers are stateless: the full transcript is rebuilt from Mongo on every
  request and nothing is stored or continued provider-side.
- `geminiMode` is optional. Allowed: `"generate_content"` (default) or
  `"interactions"`.
- `geminiTools` is optional. Allowed: `"google_search"`, `"google_maps"`,
  `"url_context"`, `"code_execution"`, `"file_search"`. If omitted/empty, Gemini uses
  the default tools: Google Search, Google Maps, and URL Context.
- `openaiThinking` is optional and only applies when `providers` includes `"openai"`.
  It maps to OpenAI Responses `reasoning`; supported fields include `effort`,
  `summary`, and `context`.
- Response: `Content-Type: text/event-stream`, status `200`.

To test Gemini Interactions code execution from the UI:
```json
{
  "prompt": "Calculate the sum of the first 50 prime numbers. Write and run Python.",
  "providers": ["gemini"],
  "sessionId": "6875e7c7289c64c5c539e021",
  "geminiMode": "interactions",
  "geminiTools": ["code_execution"]
}
```

To set OpenAI thinking/reasoning:
```json
{
  "prompt": "Compare the tradeoffs between CRDTs and OT.",
  "providers": ["openai"],
  "sessionId": "6875e7c7289c64c5c539e021",
  "openaiThinking": { "effort": "medium", "summary": "auto" }
}
```

## Projects, sessions, and turns

Projects are optional containers for sessions. Mongo ObjectIds are used for
`Project._id`, `Session._id`, `Session.project_id`, and `SessionTurn.session_id`.
There is no user ownership yet.

### Create and list projects

```http
POST /v1/cortex/projects
Content-Type: application/json

{ "name": "Research" }
```

```json
{
  "id": "6875e70b289c64c5c539e020",
  "name": "Research",
  "created_at": "2026-07-14T07:00:00Z",
  "updated_at": "2026-07-14T07:00:00Z"
}
```

`GET /v1/cortex/projects` returns projects ordered by most recently updated.

### Explicitly create a session

These routes remain available for administrative or explicit session creation. The
interactive **New session** flow should not call them; it should keep a local empty
state and let the first `/listen` request create the database session.

When the UI is inside a project, use the nested route so project membership is the
default:

```http
POST /v1/cortex/projects/6875e70b289c64c5c539e020/sessions
```

For a standalone session:

```http
POST /v1/cortex/sessions
Content-Type: application/json

{ "projectId": null }
```

Both return:

```json
{
  "id": "6875e7c7289c64c5c539e021",
  "project_id": "6875e70b289c64c5c539e020",
  "has_turns": false,
  "created_at": "2026-07-14T07:01:00Z",
  "updated_at": "2026-07-14T07:01:00Z"
}
```

- `GET /v1/cortex/sessions/{sessionId}` returns one session.
- `GET /v1/cortex/projects/{projectId}/sessions` lists that project's sessions,
  newest first.
- `PATCH /v1/cortex/sessions/{sessionId}` with `{ "projectId": null }` detaches an
  unstarted session. A different ObjectId moves an unstarted session.
- Once the first turn begins, project membership is immutable; changing or removing
  it returns `409 conflict`. Sending the same project is an allowed no-op.

### Read persisted turns

`GET /v1/cortex/sessions/{sessionId}/turns` returns every provider turn in creation
order. An invalid or unknown session returns `404`.

```json
[
  {
    "id": "6875e80d289c64c5c539e022",
    "provider": "openai",
    "parts": [
      { "type": "user", "text": "Explain CRDTs in two sentences." },
      { "type": "text", "text": "CRDTs are..." },
      { "type": "source", "url": "https://example.com" },
      { "type": "done" }
    ],
    "created_at": "2026-07-14T07:00:00Z",
    "updated_at": "2026-07-14T07:00:05Z",
    "session_id": "6875e7c7289c64c5c539e021"
  }
]
```

### ‼️ This is POST — you cannot use the browser `EventSource` API
`EventSource` only does GET. Consume this with `fetch` + a `ReadableStream` reader
(below).

### SSE frame format
Frames are separated by a blank line (`\n\n`). Two frame kinds:

**Data frame** — an `event:` line (the type) and a `data:` line (JSON `StreamChunk`).
With built-in tools on, `info` frames are interleaved with `delta` text:
```
event: info
data: {"provider":"gemini","type":"info","text":"","info":"google_search","sources":[]}

event: info
data: {"provider":"gemini","type":"info","text":"","info":"code_execution_call","toolName":"code_execution","toolInput":"print(sum(...))","toolOutput":null,"toolLanguage":"python","isError":null,"sources":[]}

event: info
data: {"provider":"gemini","type":"info","text":"","info":"code_execution_result","toolName":"code_execution","toolInput":null,"toolOutput":"5117\n","toolLanguage":null,"isError":false,"sources":[]}

event: source
data: {"provider":"gemini","type":"source","text":"","info":null,"sources":[{"url":"https://...","title":"..."}],"mapsWidgetContextToken":"..."}

event: delta
data: {"provider":"gemini","type":"delta","text":"CR","info":null,"sources":[]}

event: delta
data: {"provider":"gemini","type":"delta","text":"DTs are","info":null,"sources":[]}

event: done
data: {"provider":"gemini","type":"done","text":"","info":null,"sources":[],"mapsWidgetContextToken":null}
```

**Heartbeat frame** — a comment line (starts with `:`), sent during idle gaps to keep
the connection alive. **Ignore it.**
```
: heartbeat
```

> The `event:` line always equals `data.type`, so you can switch on either. The example
> client below reads `data` only and switches on `chunk.type`.

### Event types (`StreamChunk.type`)
| `type`   | Meaning | Payload |
|----------|---------|---------|
| `delta`  | Incremental output token(s) — append to the running text for that provider | `text` = the chunk |
| `info`   | A built-in/server-side tool event — informational, render as activity ("Searching…") | `info` = a label, e.g. `"google_search"`, `"google_maps"`, `"url_context"`, `"response.web_search_call.searching"` |
| `source` | Cited source(s) from provider grounding — render as clickable citations; Gemini Maps may also include widget context | `sources` = array of `{ url, title }`; `mapsWidgetContextToken` may be set |
| `image`  | Image(s) produced by a server-side tool (e.g. a code-interpreter matplotlib chart) — render as `<img>` | `images` = array of `{ url }`; `url` is a `data:image/png;base64,...` URI, usable directly as `img src`; `toolName` says which tool produced it |
| `done`   | That provider finished | — |
| `error`  | That provider failed mid-stream; the run continues to the next provider | `text` = error message |

Full chunk shape:
```json
{
  "provider": "openai" | "gemini" | "anthropic",
  "type": "delta|info|source|image|done|error",
  "text": "",
  "info": null,
  "toolName": null,
  "toolInput": null,
  "toolOutput": null,
  "toolLanguage": null,
  "isError": null,
  "sources": [ { "url": "https://...", "title": "..." } ],
  "images": [ { "url": "data:image/png;base64,..." } ],
  "mapsWidgetContextToken": null
}
```
Every chunk carries `provider` so you can route output to the right pane when streaming
multiple providers. `info` is non-null only for `type: "info"`; `sources` is populated
only for `type: "source"` (`title` may be `null`); `images` is populated only for
`type: "image"`. `mapsWidgetContextToken` is only expected on Gemini Maps grounding
chunks when Google Maps returns a widget token.

> ⚠️ **Display requirement — you must show the sources.** Both providers' terms require
> cited sources to be visible and clickable in the UI when grounding/search is used
> (Google grounding terms; OpenAI's web-search policy). Render `source` chunks
> as visible, clickable links pointing directly at their `url` — don't hide them or
> intercept the click. This isn't optional polish; it's a usage-policy obligation.

> **Built-in tools are on.** Each provider runs with server-side tools enabled. You
> don't execute anything — results fold into the model's `delta` text; the `info` chunks
> just tell you *what the model is doing* (so you can show a "Searching the web…" badge).
> For Gemini, the active defaults on `/v1/cortex/listen` are Google Search, Google Maps,
> and URL Context. Gemini `code_execution` and `file_search` remain backend adapter
> capabilities, but they are not enabled by this stream by default.
> Google Maps is requested with widget support enabled. When Google returns a
> Maps widget context token, it is exposed as `mapsWidgetContextToken`. Google Search
> has separate search-suggestion metadata, and URL Context has URL metadata; neither
> is exposed as a Maps widget token.

### `info` labels you'll see

Treat `info` as an **opaque activity label** — match on substring/prefix, not exact
equality (the SDKs add/rename these over time). The ones currently emitted:

**Gemini** (`info` = active generateContent metadata, or an Interactions delta kind if
that backend path is enabled later):
| label | meaning |
|---|---|
| `google_search` | Google Search grounding metadata or citations are present |
| `google_maps` | Google Maps grounding metadata or a Maps source is present |
| `url_context` | URL Context fetched/read a user-provided URL |
| `code_execution_call` | Interactions code execution generated runnable Python; render `toolInput` as code |
| `code_execution_result` | Interactions code execution returned output; render `toolOutput` and `isError` |
| SDK delta types such as `google_search_call`, `url_context_call`, `text_annotation_delta` | possible on the preserved Interactions path, not the current Vertex default |

**OpenAI** (`info` = the Responses event type; current defaults are web search and code
interpreter):
| label | meaning |
|---|---|
| `response.web_search_call.in_progress` | search started |
| `response.web_search_call.searching` | searching |
| `response.web_search_call.completed` | search done |
| `response.code_interpreter_call.in_progress` / `.interpreting` / `.completed` | code interpreter lifecycle |
| `response.code_interpreter_call_code.delta` | a fragment of the Python code being written; `toolName: "code_interpreter"`, append `toolInput` to a code block, `toolLanguage: "python"` |
| `response.code_interpreter_call_code.done` | code writing finished (lifecycle only — the full code already arrived via the deltas) |
| `response.output_item.done` + `toolName: "code_interpreter"` | execution finished; `toolOutput` = stdout/logs, `isError` set from status; any chart/image outputs follow as a separate `type: "image"` chunk |
| `response.output_item.done` + `toolName: "web_search"` | search item finished; `toolInput` = the query actually searched (or page opened), `isError` set from status |

> Substring heuristics that stay robust: `search` → searching; `code` → running code;
> `url` → fetching a page. Anything you don't recognize → a generic "working…" badge.

### What a real stream looks like

For `providers: ["gemini", "openai"]` the frames arrive **in this order** (text trimmed):

```
event: info    {provider:"gemini", info:"google_search"}          ← grounding metadata
event: delta   {provider:"gemini", text:"CRDTs ..."}              ← gemini answer streams
event: source  {provider:"gemini", sources:[{url:"...",title:"..."}]}  ← web/maps citations
event: delta   {provider:"gemini", text:" converge ..."}
event: done    {provider:"gemini"}                                ← gemini finished
: heartbeat                                                       ← (idle gap before openai)
event: info    {provider:"openai", info:"response.web_search_call.searching"}
event: delta   {provider:"openai", text:"A CRDT ..."}            ← openai answer streams
event: source  {provider:"openai", sources:[{url:"...",title:"..."}]}
event: done    {provider:"openai"}                                ← openai finished
                                                                  (connection closes)
```
`source` and `delta` interleave — citations arrive as the model emits them, not in one
batch. Accumulate them per provider.

**To listen:** accumulate `delta.text` per `provider`; collect `source` chunks into a
per-provider citation list (render clickable — required); show/clear an activity badge on
`info`; mark a pane complete on `done`; show an inline error on `error`; the whole run is
over when the connection closes.

### End of stream
There is no global "all done" event — the **connection closing** signals the whole run
is over. Each provider emits its own `done` along the way.

### Errors in the stream
- **Before streaming starts** (e.g. `422` bad body): a normal HTTP error response — no
  SSE.
- **After streaming starts** (status `200` already sent): a provider failure arrives as
  `event: error` with the message in `data.text`. The HTTP status stays `200`. Surface
  the error for that provider; other providers in the list still run.

---

## Reference client (TypeScript, `fetch` + stream reader)

```ts
type Provider = "openai" | "gemini" | "anthropic";
type Source = { url: string; title: string | null };
type ImageOutput = { url: string };
type StreamChunk = {
  provider: Provider;
  type: "delta" | "info" | "source" | "image" | "done" | "error";
  text: string;
  info: string | null;
  toolName: string | null;
  toolInput: string | null;
  toolOutput: string | null;
  toolLanguage: string | null;
  isError: boolean | null;
  sources: Source[];
  images: ImageOutput[];
  mapsWidgetContextToken: string | null;
};

async function cortexListen(
  baseUrl: string,
  body: {
    prompt: string;
    providers: Provider[];
    sessionId?: string;
    projectId?: string;
  },
  onChunk: (chunk: StreamChunk) => void,
  signal?: AbortSignal, // pass to cancel the stream (closes the connection)
): Promise<string> {
  const res = await fetch(`${baseUrl}/v1/cortex/listen`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });

  if (!res.ok) {
    // Pre-stream error (e.g. 422 validation). Parse and throw.
    throw new Error(`cortex/listen failed: ${res.status} ${await res.text()}`);
  }
  const sessionId = res.headers.get("X-Session-ID");
  if (!sessionId) throw new Error("cortex/listen response is missing X-Session-ID");
  if (!res.body) throw new Error("No response body");

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break; // connection closed = whole run finished
    buffer += decoder.decode(value, { stream: true });

    // Frames are separated by a blank line.
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);

      // Heartbeat / comment frame — ignore.
      if (frame.startsWith(":")) continue;

      // Pull the `data:` line out of the frame (ignore the `event:` line —
      // chunk.type carries the same info).
      const dataLine = frame
        .split("\n")
        .find((l) => l.startsWith("data:"));
      if (!dataLine) continue;

      const chunk: StreamChunk = JSON.parse(dataLine.slice(5).trim());
      onChunk(chunk);
    }
  }
  return sessionId;
}
```

### Usage
```ts
const textByProvider: Record<string, string> = {};
const controller = new AbortController();

const sessionId = await cortexListen(
  "http://localhost:8000",
  { prompt: "Explain CRDTs in two sentences.", providers: ["gemini", "openai"] },
  (chunk) => {
    if (chunk.type === "delta") {
      textByProvider[chunk.provider] = (textByProvider[chunk.provider] ?? "") + chunk.text;
      render(chunk.provider, textByProvider[chunk.provider]);
    } else if (chunk.type === "info") {
      showActivity(chunk.provider, chunk.info); // e.g. "google_search"
    } else if (chunk.type === "source") {
      addSources(chunk.provider, chunk.sources); // render as clickable citations (required)
    } else if (chunk.type === "error") {
      showError(chunk.provider, chunk.text);
    } else if (chunk.type === "done") {
      markDone(chunk.provider);
    }
  },
  controller.signal,
);

// Store sessionId in chat state and include it in every later cortexListen body.

// To cancel mid-stream: controller.abort();  (the server stops the run cleanly)
```

### Quick check with curl
```bash
curl -N -X POST http://localhost:8000/v1/cortex/listen \
  -H 'content-type: application/json' \
  -d '{"prompt":"hello","providers":["gemini"]}'
```
`-N` disables curl's buffering so you see frames arrive live.

---

## Notes / gotchas for the implementer

- **Buffer across reads.** A single `reader.read()` may contain a partial frame or
  several frames — accumulate in a buffer and split on `\n\n` (the example does this).
- **Don't rely on `EventSource`.** POST + body rules it out; use the fetch reader.
- **Heartbeats are noise** — any frame starting with `:` is a keep-alive; skip it.
- **`info` is presentational, not content** — never append `chunk.info` to the answer
  text; it's an activity signal. Match it loosely (substring) and clear the badge when
  that provider's next `delta` or `done` arrives.
- **Cancellation:** abort the fetch (`AbortController`) to stop a run; the server
  cancels the in-flight generation.
- **No auth on `cortex/listen` yet** — it's currently open. If/when it's protected,
  add `Authorization: Bearer <token>` like the user routes.
- **Multiple providers = sequential**, tagged by `chunk.provider`; render into separate
  panes keyed by provider.
