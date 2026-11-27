# Gems

A gem is one of the user's assistants. Cortex is the name of the project, which holds
the gems, not of an assistant. Every call and every written reply is with a gem.

## A gem's config

Each gem has one config, kept in the `Gems` collection but treated like a config file.
It is the one home of a gem: what a call or a chat acts on, and what the app shows of
it. It is in [`app/entities/gem_ent.py`](../app/entities/gem_ent.py).

| Field | Meaning |
| --- | --- |
| `version` | the version of the config's shape, 3 today. Every stored config carries it |
| `name` | what the gem is called: required, trimmed, at most 100 characters |
| `description` | one line on what the gem is for, shown under its name. The model is not told it. May be empty; at most 200 characters |
| `image` | the gem's picture, as one value the backend does not read: today the app's built-in face and colour, later perhaps the address of a picture. Null for none; at most 500 characters |
| `instructions` | plain text saying who the gem is and how it behaves. A gem made with none, or blank ones, is given the default instructions: how to talk in a call and how to write. After that they are stored as they are sent, so blank means the user removed them. At most 20,000 characters |
| `model` | a `name` from `GET /v1/voice/models`, or null for the configured default (`VOICE_MODEL`). A stored name that is no longer offered is read as null |
| `speaks_first` | whether the gem greets the user when a call connects. Default false |
| `tools` | the names of the built-in tools it may use, no name twice. Default `["web_search"]`, the only one there is today. Any other name is refused when a gem is made or changed. A connection's tools are not named here: who may use them is said on the connection (see [Connections](connections.md#who-may-use-what)) |
| `voice` | the voice its replies are spoken in, as the app names it. Null for none chosen; at most 100 characters. Kept, but a call does not use it yet |
| `speech_speed` | how fast it is spoken, 0.5 to 2.0. Default 1.0. Kept, but a call does not use it yet |

A gem is offered `web_search` only if its `tools` name it and `PARALLEL_API_KEY` is set,
and with it the tools of the connections open to it. With any tool comes
`cancel_tool_call`. A stored config that names a tool which has since gone still loads:
it is just not offered.

### Versions

Something new (skills, say) is added by raising the version and writing one step that
brings older configs up to the new shape (`upgrade_gem_document`):

- **Version 1** is the first shape. A config with no version is read as version 1.
- **Version 2** added `description`, `image`, `voice` and `speech_speed`. A version 1
  config reads with nothing chosen for them.
- **Version 3** made the rules for a call and for writing part of a gem's instructions,
  where the code used to add them. An older config's instructions read as its own text,
  a blank line, then the default instructions (or the defaults alone if it had none).

An older config is stored in the new shape the next time it is changed, whatever the
change. A config from a newer version than the code knows is refused: a call with that
gem gets `call.failed`, and a message `chat.error`.

## What a gem is told

What a gem is told is one text, the same in a call and in writing, whichever tools it is
offered. It is put together in
[`app/entities/instructions.py`](../app/entities/instructions.py), from what changes
least to what changes most:

1. **The Darbar text**: the environment, the same for every gem (below).
2. **"You are {name}."** and the gem's own instructions. By default these say how it
   talks in a call and how it writes.
3. **What the model itself needs**: on an Anthropic model, the line that explains
   `<note>` tags.

The order keeps the start of every request the same for as long as possible, which is
what a prompt cache needs. **Nothing in it changes by itself**: the instructions change
only when the user edits something. What changes as the conversation goes is said by
lines of ours in the conversation instead:

- whether it is spoken or written: "The user has called you. From here on the
  conversation is spoken." before every call's first turn, and "The call has ended.
  From here on the conversation is written." before the first written turn after a
  call;
- the date and the time, in the user's time zone (see
  [the contract](chat-contract.md#the-time)).

The texts in full, as the code has them, are in
[the spec](voice-spec.md#the-instructions).

## The Darbar text

The Darbar text is what every gem is told first: what a call and writing are, what a
reply ending in "…" means, and what a late tool result is. It is a setting the user can
change, in [`app/entities/darbar_ent.py`](../app/entities/darbar_ent.py).

- **Until the user changes it**, it is a built-in text (`DEFAULT_DARBAR`): a draft the
  owner has yet to read.
- **A text the user writes takes its place whole**, so a gem told an empty or custom one
  is not told what the built-in one says. An empty one means gems are told nothing
  there.
- It is kept as one document in the `Darbar` collection, at most 4,000 characters.
- **When a change applies.** A call reads it once, when it opens, and keeps that text to
  its end, so a change reaches the next call, never one already open. A written reply
  reads it each time, so a change applies from the next message.

## Routes

| Route | Does |
| --- | --- |
| `POST /v1/gems` | makes a gem (201) |
| `GET /v1/gems` | lists them in the order they were made. Changing a gem does not move it |
| `GET /v1/gems/{gem_id}` | one gem |
| `PATCH /v1/gems/{gem_id}` | changes only the fields sent. Only `model`, `image` and `voice` may be null |
| `DELETE /v1/gems/{gem_id}` | deletes it (204), with its conversation and its log. It is taken out of every connection that names it |
| `GET /v1/gems/{gem_id}/chat` | its conversation so far, calls included |
| `POST /v1/gems/{gem_id}/reset` | resets it (204): it starts again, knowing nothing from before. Its config and its log stay. 409 while it is in a call (see [the contract](chat-contract.md#post-v1gemsgem_idreset)) |
| `POST /v1/gems/{gem_id}/compact` | compacts its conversation now, as only the user does: everything it remembers so far is sent as a summary from then on (200, with `summary` and `through`). 409 in a call or with nothing to compact, 502 if the summary could not be written, 500 if it could not be stored (see [the contract](chat-contract.md#post-v1gemsgem_idcompact)) |
| `GET /v1/gems/{gem_id}/context` | where its context stands: its estimated size, when its cache expires, when to offer a compaction, and what keeping the conversation and compacting it would cost (see [the contract](chat-contract.md#get-v1gemsgem_idcontext)) |
| `GET /v1/gems/{gem_id}/log` | its log (see [Observability](observability.md)) |

The Darbar text is read and set at `/v1/darbar` (see
[the contract](chat-contract.md#the-darbar-text-v1darbar)). Ids go out as strings, and a
missing or malformed id is a 404. There is no sign-in on
any of these: this backend has one user for now. What the app reads from them is in
[the contract](chat-contract.md).
