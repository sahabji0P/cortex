# The standing socket: the contract with the app

This is the one place that says how the app and the backend talk over the standing
socket: written chat, calls, and the Darbar text that goes with them. The backend is
built to it and so is the app. Change it here first.

The names of the routes (`/v1/link`, `/v1/darbar`) are working names.

## What stays as it is

- Gems: `/v1/gems`, over plain HTTP.
- The old call socket, `/v1/voice/converse`, opened per call. It works exactly as it did,
  with the same query, frames and close codes, until the app has moved its calls onto
  the standing socket. Then it will be removed.

Plain HTTP is for saving and loading things. The standing socket is for what is live.

## The standing socket: `/v1/link`

The app opens one WebSocket to `/v1/link` when it starts and keeps it open. If it drops,
the app opens it again. It carries written chat and calls. Every text frame is JSON with
a `type`. Binary frames are a call's audio.

### The app sends

| `type` | Fields | Meaning |
| --- | --- | --- |
| `chat.send` | `gem` (a gem's id), `text` | A message to that gem. If a reply to that gem is still being written, that reply is cut first (see `chat.cut`) |

### The backend sends

| `type` | Fields | Meaning |
| --- | --- | --- |
| `link.ready` | `version` (3) | Sent once, as soon as the socket is open. Version 2 carries calls. Version 3: a search carries on after a cut, and its result is told later |
| `chat.result` | `gem`, `text` | A search the gem started in an earlier reply has come back, and a reply about it follows, unasked. `text` is what the search looked up, as its `chat.tool` said, or "" |
| `chat.text` | `gem`, `text` | The next piece of the reply, as it is written |
| `chat.tool` | `gem`, `text` | The gem is using a tool. `text` says what, such as what a search looks up |
| `chat.done` | `gem`, `text` | The reply is complete. `text` is the whole reply as it is kept: its stretches of text, joined by a space |
| `chat.cut` | `gem`, `text` | The reply was cut off by a new message. `text` is what is kept of it: what was sent, ending in "…". If it was cut while a tool was running, it is the text before the tool, whole, and the tool carries on: its result comes later (see `chat.result`). If nothing is kept, it is just "…" |
| `chat.error` | `gem`, `text` | No reply can be made, or the reply failed: the gem is not there, its model has no key, or the model call failed. `text` is the reason. What was sent before a failure is kept, ending in "…" |
| `error` | `text` | A frame the backend could not read (not JSON, or a `type` it does not know), a message that is blank or longer than 20,000 characters, or a `call.start` while a call is open. Nothing is sent to the gem, no running reply is cut, and a running call carries on. The socket stays open |

One reply at a time for each gem, whichever socket asked. A `chat.send` that is accepted
gets `chat.text` and `chat.tool` frames in the order they happen, then exactly one of
`chat.done`, `chat.cut` or `chat.error`. After a
`chat.cut`, the frames that follow for that gem belong to the new message.

A `chat.send` is not the only thing that starts frames for a gem. A search still running
when its reply is cut carries on. When it finishes, at the next moment no reply is being
written to that gem, the gem tells the user what it found in a reply of its own, with no
new message: one `chat.result` for each search that came back, then `chat.text` and
`chat.tool` frames, then exactly one of `chat.done`, `chat.cut` or `chat.error`. A
search that finishes while a reply is being written waits until that reply is stored,
and several that are waiting are told in one reply. Show it as a reply of the gem's,
under a line saying a search came back.

That reply goes to the socket that sent the latest message to that gem, and nowhere
else. If that socket has closed, nothing is sent and no reply is written: the result is
only stored, and the gem brings it up when it next answers. A new message cuts that
reply like any other, and is answered once it is stored. So does that socket closing
while it is being written: it is kept as what was sent, ending in "…".

How the app tells such a reply from the reply to a message: it always begins with
`chat.result`, and everything for that gem from there to the next `chat.done`,
`chat.cut` or `chat.error` belongs to it, even if the app has sent a message in the
meantime. That message's own frames follow that last frame. A `chat.result` is never
sent in the middle of a reply.

A reply's text comes in stretches. What a gem writes before it uses a tool is one
stretch, and what it writes after is another: start a new line after a `chat.tool`. The
history keeps each stretch as a message of its own.

Nothing is kept of a message that gets `chat.error` because the gem is not there, its
config cannot be read, or its model has no key. When a reply fails part-way, the user's
message and what was sent of the reply are kept.

A reply to a message is sent to the socket that sent it. Another socket open on the same
gem is not told: it reads the history again. If that socket closes while a reply is
being written, the reply is cut, as if a new message had arrived, and a search it was
running carries on.

## Calls

A call goes over the same socket. Its audio is binary frames; everything else is JSON
text frames like the rest.

### The app sends

| Frame | Fields | Meaning |
| --- | --- | --- |
| `call.start` (JSON) | `gem` (optional gem id), `model` (optional, a name from `GET /v1/voice/models`), `first` (optional, `user` or `assistant`) | Open a call. The three fields mean exactly what `?gem=`, `?model=` and `?first=` mean on the old socket, including that a `model` or `first` that is sent wins over the gem's and one left out leaves the gem's |
| binary | raw 16-bit mono PCM at 16 kHz, about 100 ms a frame | Mic audio for the open call. Audio sent after `call.start` and before `call.ready` is kept and fed in order once the call is open: none is lost. A binary frame when no call is open or opening is dropped without an answer |
| `call.playback` (JSON) | `delay` (seconds, >= 0) | The sound device's delay, as `playback` on the old socket |
| `call.end` (JSON) | none | Hang up |

### The backend sends

| Frame | Fields | Meaning |
| --- | --- | --- |
| `call.ready` (JSON) | none | The call is open |
| binary | raw 16-bit mono PCM at 24 kHz | Speech audio of the open call |
| `call.user.partial`, `call.user.final`, `call.assistant.delta`, `call.assistant.done`, `call.tool.call`, `call.interrupt`, `call.error` (JSON) | `text` | Each is a `VoiceEvent` with `call.` put before its type; `text` is unchanged. `call.error` is something that failed inside a call that carries on, as `error` was |
| `call.ended` (JSON) | `text` | The call is over: the app hung up, or the conversation ended by itself. `text` is empty |
| `call.refused` (JSON) | `text` (the reason) | The call was not opened because of what was asked. This replaces close code 1008: unknown gem, an id that is not an id, a model or setting that is not offered, a model whose provider has no key. Same reasons in words as today |
| `call.failed` (JSON) | `text` (the reason) | The call could not be opened, or broke, through a fault on the backend's side. This replaces close code 1011: the gem's config or the Darbar text cannot be read, speech services unavailable, a failure mid-call, or mic audio that is not being taken (see the rules) |

What each event means is in the README, under the old socket's contract. On
`call.interrupt`, stop playback and drop any audio not yet played.

### Rules

- One call at a time on a socket. Binary frames carry no tag: they belong to that
  socket's one call.
- A `call.start` that is accepted is followed by either `call.ready`, then events and
  audio in the order the conversation produced them, then exactly one of `call.ended` or
  `call.failed`; or, with no `call.ready`, exactly one of `call.refused` or
  `call.failed`, or `call.ended` if the app hung up while the call was opening. After
  that frame nothing more is sent for that call, binary included, and a new `call.start`
  may be sent.
- The socket stays open through all of it. A refused or failed call never closes the
  link, and never disturbs a chat reply being written on it. A failed chat reply never
  disturbs a call.
- A `call.start` while a call is open or opening on this socket gets the general
  `{"type": "error", "text": "a call is already open"}` frame and the running call
  carries on. That start gets no other frame. It is not `call.refused`, because a
  call's frames carry no tag: a refusal arriving in the middle of the running call
  could be taken for that call's last frame. The app may rely on those exact words. `call.end` and
  `call.playback` with no call are ignored. A `call.start` sent right after a `call.end`
  is accepted, and its frames come after the first call's `call.ended`.
- A `call.end` while the call is still opening stops the opening and gets `call.ended`.
- The link closing ends its call, as it cuts its chat replies.
- Mic audio is fed into the call in the order it came. If speech recognition stops
  taking it and about half a minute of it is waiting, the call is failed with
  `call.failed`. The old socket made the app wait instead.
- A client that stops reading its frames for ten seconds counts as gone: the socket is
  closed on it, which ends its call and cuts its replies.
- Calls changed nothing about chat frames (`chat.send` and the `chat.*` frames back).

A `call.start` whose `model` or `first` is not offered gets `call.refused` with "unknown
model or setting", not `error`. A `call.playback` that cannot be read, such as one with a
negative delay, gets `error`.

## What a gem remembers of a chat

The backend keeps each gem's written conversation in the database: what the user wrote,
what the gem wrote as far as it was sent, and its tool calls. It is only ever added to.
Every reply is made from the gem's config and the Darbar text as they are at that moment,
so a change to either applies from the next message.

A search cut off while it runs is kept with a result saying it is still running. When it
comes back, its real result is added after everything else, as a second call that names
the first. The gem can stop a search still running. Searches run in the backend's memory,
so a restart loses them: before the next reply, a search lost that way is added a result
saying so.

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
  carried. The call that brings a late result, and a call that stops a search, are not
  shown. A reply about a late result is a `gem` message like any other.
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
