# Conversation Persistence and Hierarchy

Status: **Write persistence and model-context replay implemented.**
Last updated: 2026-07-19

## Current hierarchy

MongoDB stores three collections with ObjectId references:

```text
Project (optional container)
  └── Session
        └── SessionTurn (one per provider run)
```

No user identity or ownership is stored yet.

### `Projects`

```text
_id         ObjectId
name        str
created_at  datetime
updated_at  datetime
```

### `Sessions`

```text
_id         ObjectId
project_id  ObjectId | null
has_turns   bool
created_at  datetime
updated_at  datetime
```

`project_id` can be changed or cleared only while `has_turns` is false. The update is
conditional in Mongo, so a concurrent first turn prevents a project detach or move.

### `SessionTurns`

```text
_id         ObjectId
provider    str
parts       list[dict[str, str]]
request_id  ObjectId | null  # null only on legacy documents
provider_response_id  str | null
provider_parent_response_id  str | null
created_at  datetime
updated_at  datetime
session_id  ObjectId
```

Every turn requires an existing session. The first turn atomically marks the session
as started before the turn is inserted. `parts` begins with the exact user input as a
`{"type": "user", "text": "..."}` part, followed by the provider output. Stream text
deltas are compacted into text parts; tool, source, image, error, and completion parts
retain their ordered scalar payload. Partial output is saved when a client cancels the
stream.

`request_id` groups the provider fan-out for one `/listen` request, so the same user
input is replayed once even when several providers answered it. Legacy records without
`request_id` are grouped deterministically only while consecutive provider turns share
the same prompt; records without a persisted user prompt cannot be replayed.
`provider_response_id` and `provider_parent_response_id` are internal continuation
metadata for OpenAI and Gemini Interactions and are not part of the frontend turn
response.

## Indexes

- `Project`: `(updated_at, -1)`
- `Session`: `(project_id, 1), (updated_at, -1)`
- `SessionTurn`: `(session_id, 1), (created_at, 1)`

The reference fields are actual BSON ObjectIds, not strings.

## HTTP lifecycle

- `POST /v1/cortex/projects`
- `GET /v1/cortex/projects`
- `POST /v1/cortex/projects/{projectId}/sessions`
- `GET /v1/cortex/projects/{projectId}/sessions`
- `POST /v1/cortex/sessions` for a standalone or explicitly assigned session
- `GET /v1/cortex/sessions/{sessionId}`
- `PATCH /v1/cortex/sessions/{sessionId}` to move/detach an unstarted session
- `GET /v1/cortex/sessions/{sessionId}/turns`
- `POST /v1/cortex/listen`; pass an existing `sessionId`, or omit it to create the
  session on the first submitted turn. `projectId` optionally places a new session
  inside an existing project.

## Model context and compaction

Before starting a new provider run, the backend loads all ordered turns for the
session and builds one user/assistant exchange per `request_id`. Runs for the same
session are serialized within one application process. Persisted parent continuation
ids also detect branches created by concurrent workers; the next request then rebuilds
from Mongo instead of continuing either incomplete branch. For each historical request
the backend prefers a completed, error-free answer from the provider currently being
called. If that provider failed or was cancelled, it falls back to the first completed
provider answer. Partial output remains visible in stored turns but is not replayed as
a completed assistant message.

The normalized exchanges are converted at the adapter boundary into native OpenAI
Responses input messages, Anthropic Messages, Gemini `Content` values, or Gemini
Interactions steps.

OpenAI stores each response id on its turn. When the immediately preceding request has
an OpenAI response id, the next call uses `previous_response_id` and sends only the new
user input. Every OpenAI call enables server-side native compaction with
`compact_threshold: 150000` and `store: true`. If the stored response is expired,
missing, or invalid, the backend retries once with the normalized Mongo history and
begins a new chain. The retry is allowed only before any new output has streamed.

Gemini Interactions follows the same durable pattern with `store: true` and
`previous_interaction_id`. Completed interaction ids are saved on their turns. An
expired or invalid interaction causes one pre-output retry with normalized Mongo history
and the replacement interaction id is persisted. Gemini `generateContent` and
Anthropic Messages remain stateless and receive the complete normalized history on
every request. No application-side summarization or silent truncation is applied to
those paths.

Mongo is the durable source of truth and currently has no conversation TTL. Provider
continuation objects are treated only as optimizations because their retention windows
can expire. Full replay is still subject to each provider/model's request context limit;
there is not yet a durable application-generated summary checkpoint for sessions that
outgrow that limit.
