# Chat and the Darbar text: the contract with the app

This is the one place that says how the app and the backend talk about written chat and
the Darbar text. The backend is built to it and so is the app. Change it here first.

The names of the routes (`/v1/link`, `/v1/darbar`) are working names.

## What stays as it is

- A call: its own WebSocket at `/v1/voice/converse`, opened per call. Nothing about it
  changes here. It moves onto the standing socket in a later step.
- Gems: `/v1/gems`, over plain HTTP.

Plain HTTP is for saving and loading things. The standing socket is for what is live.

## The standing socket: `/v1/link`

The app opens one WebSocket to `/v1/link` when it starts and keeps it open. If it drops,
the app opens it again. Today it carries written chat. Every frame is a JSON text frame
with a `type`.

### The app sends

| `type` | Fields | Meaning |
| --- | --- | --- |
| `chat.send` | `gem` (a gem's id), `text` | A message to that gem. If a reply to that gem is still being written, that reply is cut first (see `chat.cut`) |

### The backend sends

| `type` | Fields | Meaning |
| --- | --- | --- |
| `link.ready` | `version` (1) | Sent once, as soon as the socket is open |
| `chat.text` | `gem`, `text` | The next piece of the reply, as it is written |
| `chat.tool` | `gem`, `text` | The gem is using a tool. `text` says what, such as what a search looks up |
| `chat.done` | `gem`, `text` | The reply is complete. `text` is the whole reply as it is kept: its stretches of text, joined by a space |
| `chat.cut` | `gem`, `text` | The reply was cut off by a new message. `text` is what is kept of it: what was sent, ending in "…". If it was cut while a tool was running, it is the text before the tool, whole. If nothing is kept, it is just "…" |
| `chat.error` | `gem`, `text` | No reply can be made, or the reply failed: the gem is not there, its model has no key, or the model call failed. `text` is the reason. What was sent before a failure is kept, ending in "…" |
| `error` | `text` | A frame the backend could not read, or a message that is blank or longer than 20,000 characters. Nothing is sent to the gem and no running reply is cut. The socket stays open |

One reply at a time for each gem, whichever socket asked. A `chat.send` that is accepted
gets `chat.text` and `chat.tool` frames in the order they happen, then exactly one of
`chat.done`, `chat.cut` or `chat.error`. After a
`chat.cut`, the frames that follow for that gem belong to the new message.

A reply's text comes in stretches. What a gem writes before it uses a tool is one
stretch, and what it writes after is another: start a new line after a `chat.tool`. The
history keeps each stretch as a message of its own.

Nothing is kept of a message that gets `chat.error` because the gem is not there, its
config cannot be read, or its model has no key. When a reply fails part-way, the user's
message and what was sent of the reply are kept.

A reply is sent to the socket that asked for it. Another socket open on the same gem is
not told: it reads the history again. If that socket closes while a reply is
being written, the reply is cut, as if a new message had arrived.

## What a gem remembers of a chat

The backend keeps each gem's written conversation in the database: what the user wrote,
what the gem wrote as far as it was sent, and its tool calls. It is only ever added to.
Every reply is made from the gem's config and the Darbar text as they are at that moment,
so a change to either applies from the next message.

A call does not read or add to this yet: a call still starts empty. Joining the two into
one context is a later step.

### `GET /v1/gems/{gem_id}/chat`

The written conversation so far, oldest first, for showing on screen:

```json
{
  "messages": [
    {"role": "user", "text": "what is my balance?", "at": "2026-11-17T08:00:00Z"},
    {"role": "tool", "text": "current balance of the savings account", "at": "2026-11-17T08:00:01Z"},
    {"role": "gem", "text": "It is forty thousand rupees.", "at": "2026-11-17T08:00:03Z"}
  ]
}
```

- The order of the list is the order of the conversation. `at` is when each message was
  stored, to show a time by: a user's message carries when it arrived, and the messages
  of one reply all carry when the reply ended. Do not sort by it.
- `role` is `user`, `gem` or `tool`. A `tool` message's `text` is what a `chat.tool` frame
  carried.
- A reply that was cut mid-text ends in "…". One cut before any of it was sent is not
  there at all.
- A gem that is not there is a 404.

Deleting a gem deletes its written conversation.

## The Darbar text: `/v1/darbar`

The Darbar text is the first thing every gem is told, ahead of its own name and
instructions: what is true for every gem. The user can change it. It applies to every
conversation opened after the change. A call without a gem is not sent it.

| Route | Does |
| --- | --- |
| `GET /v1/darbar` | `{"text": "...", "is_default": true}`. `is_default` says whether it is the built-in text |
| `PUT /v1/darbar` | Body `{"text": "..."}`, at most 4,000 characters. An empty text means gems are told nothing here. Answers like `GET` |
| `DELETE /v1/darbar` | Goes back to the built-in text. Answers like `GET` |
