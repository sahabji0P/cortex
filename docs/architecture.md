# Architecture

How the backend is built: its layers, the loop every reply runs, the one conversation a
gem has, and what it remembers. For what the app sends and receives, see
[the contract](chat-contract.md). For a call in full detail, see
[the voice spec](voice-spec.md).

## Layers

Imports only go one way:

```text
config ← core ← adapters ← entities ← routers ← main
```

- **`config`** reads the settings from the environment.
- **`core`** holds what every layer may use: errors, password and token helpers, and how
  times are kept and sent.
- **`adapters`** are one file per outside service, and the only place an SDK is called.
  Every call to the outside is async.
- **`entities`** hold the logic. They reach the outside only through adapters.
- **`routers`** are the HTTP and WebSocket endpoints, one folder each, with a
  `controller.py` (the routes), `models.py` (what goes in and out) and `service.py`.
- **`main`** builds the app and includes every router.

## Where the code is

### Adapters

| File | Service |
| --- | --- |
| `openai_adp.py` | OpenAI, with the API key or through the proxy |
| `anthropic_adp.py` | Anthropic's Claude API |
| `cartesia_stt_adp.py` | Cartesia speech-to-text, which also detects turns |
| `cartesia_tts_adp.py` | Cartesia text-to-speech, with each word's timing |
| `parallel_adp.py` | Parallel web search |
| `mcp_adp.py` | MCP tool servers, over Streamable HTTP, with their sign-in |
| `cliproxy_adp.py` | CLIProxyAPI's management API: the proxy's ChatGPT logins |
| `docdb_adp.py` | MongoDB |
| `redis_adp.py` | Redis: only the user login writes to it |
| `http_adp.py` | a shared HTTP client |

### Entities

| File | What it holds |
| --- | --- |
| `loop.py` | the loop that makes one reply, on any surface, and the hooks a record of it uses |
| `voice_ent.py` | a call's conversation: the cascade, interruption, and when a late result is told |
| `voice_speech.py` | speaking a reply, working out how much of it was heard, and timing the words the app shows |
| `written_ent.py` | a conversation in writing: one message in, its reply streamed out |
| `call_ent.py` | opening a call, and storing what it adds |
| `chat_ent.py` | written chat with the gems, made from what is stored |
| `conversation_ent.py` | what is kept in memory of each gem's one conversation: its writer and its background calls; loading and adding to what is stored |
| `conversation_store.py` | each gem's log in the database (`GemLog`) |
| `history.py` | the rules for what joins a history and when: the user's words, the lines where the mode changes, what is kept beside an item |
| `late_results.py` | tool calls that outlive their reply, held until their results are told: one copy for a call and for chat |
| `gem_log.py` | everything that happens with a gem, numbered and written down as it happens |
| `model.py` | either provider's model behind one interface, and the list of models |
| `prices.py` | what a model request cost, from its tokens |
| `providers_ent.py` | how OpenAI is reached (the API key or the proxy), and the proxy's account |
| `instructions.py` | what the model is told, in layers |
| `tools.py` | the tools a model may call, each a small object, and which a gem is offered |
| `connection_ent.py` | the tool servers, which gem may use which, and signing in to them |
| `gem_ent.py` | a gem's config and its versions |
| `darbar_ent.py` | the Darbar text every gem is told first |
| `user_ent.py` | the user account behind `/v1/users` |

### Routers

`link` (the standing socket, which carries chat, calls and the log), `gems`, `darbar`,
`connections`, `providers`, `voice` (the model list) and `user`.

## The loop, and the surface a reply is delivered on

Making a reply is the same whether the gem was called or written to. The loop in
[`loop.py`](../app/entities/loop.py) runs one: a model round, then the tool calls it
made, then their results back to the model in the next round, until a round only says
something. There is no limit on the number of rounds. Every gem runs this one loop. What
differs is its config and the surface its reply is delivered on.

A surface takes the reply's text and can say how much of it got through by a given
moment.

- **Speech**, in a call: what got through is what was heard. The model's text goes to
  text-to-speech as it streams, and the audio to the app as it is made.
- **Writing** ([`written_ent.py`](../app/entities/written_ent.py)): what got through is
  what the reader was handed. One message in, its reply streamed out as text, one reply
  at a time; or a reply with no new message, written from the history as it stands.

The history holds what was delivered, by the rules below, on any surface.

## One conversation

A gem has one conversation, whether the user calls it or writes to it. A call starts
from the gem's conversation as it is stored, as a written reply does, and a written
reply after a call is made from everything the call added. The only difference is the
speech around the same loop.

**The database is the truth, not the connection.** Every reply is made from what is
stored: the gem's config and the Darbar text as they are now (a call reads them once,
when it opens), and its conversation so far. Little is kept in memory between replies:
the reply in progress, and the tool calls still running. So a changed gem applies from
the next message, two windows on one gem cannot drift apart, and a dropped connection
loses nothing: a socket that closes cuts its replies, and they are stored as cut.

- **Everything is stored the moment it is final.** A message is stored when its reply
  begins, whatever becomes of the reply. A call stores what it adds as it goes. A reply
  that can still be cut is never stored. Everything is stored before a reply's last
  frame, or a call's. If it cannot be, the reply ends in `chat.error` and the call in
  `call.failed`, and what could not be stored is dropped, never stored later.
- **One writer at a time.** A call holds the gem from when speech is connected until
  what it added is stored. Meanwhile a message to the gem gets `chat.error` ("the gem is
  in a call"), and another `call.start` for it gets `call.refused` with the same words.
  A `call.start` while a reply is being written cuts that reply, and the call opens once
  it is stored.
- **A new message cuts the reply still being written** to that gem, like speaking over
  someone in a call. This holds across every socket of the process: one reply at a time
  for each gem.
- **One set of background calls.** A tool call left running by a written reply whose
  result comes in during a call is told in the call. One still running when a call ends
  carries on, and its result is told in writing, to the socket of the latest message,
  or only stored if there is none.
- **Calls lost to a restart.** Background calls run in the process's memory, so a
  restart loses them. Before the next reply, written or in a call, each one lost is
  closed with a stub call and a result saying it was lost and will not arrive.
- **The whole conversation is sent every time.** Nothing is trimmed or summed up, and
  old results are not cleared. The context grows with every message, and quickly when
  tools are used. A message is at most 20,000 characters, so one stray paste cannot ride
  along in every request after it.
- **Deleting a gem** deletes its conversation and its log, and a background result of a
  deleted gem is dropped.

The writer and the background calls of each gem are kept in
[`conversation_ent.py`](../app/entities/conversation_ent.py). A call is opened in
[`call_ent.py`](../app/entities/call_ent.py), and written chat is
[`chat_ent.py`](../app/entities/chat_ent.py).

## What a gem remembers

The history is what the model is sent on every request: the gem's stored conversation.
Nothing is stored at the provider. It follows three rules, the same in a call and in
writing.

1. **It holds what was delivered, not what was generated.** A reply joins the history
   once it has been heard (or read) in full or cut off. A cut reply is stored as the
   words that got through, ending in "…", and the rest is never sent back. Nothing is
   added about the unheard part: the "…" and the instructions say it. A reply cut
   before any of it got through leaves no reply. In a call, how much was heard is worked
   out on the server from the speech service's word timings and a copy of the app's
   playback schedule (see [the spec](voice-spec.md#output-path-and-interruption)).
2. **Every tool call keeps a valid result.** An interruption does not stop a tool call.
   Its call gets a placeholder result and carries on in the background. When the real
   result comes in, it is added as a stub call written by the server, naming the
   original, followed by the result, and the gem tells the user in a reply of its own.
   If the user's turn ends while a result is waiting, the result goes in ahead of their
   words and one reply does both. The model can stop a running call with
   `cancel_tool_call`. The same code does this for a call and for chat
   ([`late_results.py`](../app/entities/late_results.py)); the spec has
   [every case](voice-spec.md#tool-calls-under-interruption).
3. **Nothing the model has been sent is changed afterwards.** Everything joins the
   history the moment it is final, before the request that carries it, so each request
   starts with the whole of the one before it. The instructions and the tool list stay
   the same, so a prompt cache stays valid. One consequence: what the model said before
   a tool call is kept whole once it is sent back with the result, even if the user cut
   it off part-way.

**What the user says is stored as it was said, as a message of its own.** Words said, or
a message sent, before a reply to the one before has got through are one more user
message after it, and the next reply answers both. The rule is one, for a call and for
writing, in [`history.py`](../app/entities/history.py).

**Stored items say what they are.** Every item says its mode (`call` or `chat`) and who
wrote it (`source`): the user, the gem, a tool, or one of our own lines and stubs. A
placeholder, and the answer of a `cancel_tool_call` that stopped a call, carry a `mark`
that says so, and are found by it, never by their wording. A stub says which call it
delivers (`delivers`). The fields are in
[the contract](chat-contract.md#what-the-gem-remembers).

## The log

Everything that happens with a gem is written down in one list, in order: what it
remembers, and every turn, request, tool call and change in between. What the gem
remembers is those entries. The log is the base for anything that looks at what a gem
did, and has a page of its own: [Observability](observability.md).
