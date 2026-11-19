# Cortex

Cortex is a personal experimentation backend. Its current experiment is a spoken conversation built as a cascade: speech-to-text, then a model, then text-to-speech, with interruption handling and a web search tool. A gem can also be written to (see [Chat](#chat)). Called or written to, a gem has one conversation, and it is kept (see [One conversation](#one-conversation)).

## The voice cascade

```text
mic audio ──▶ speech-to-text ──▶ model ──▶ text-to-speech ──▶ speech audio
              Cartesia ink-2     OpenAI or Cartesia Sonic
                   │             Anthropic
                   │                │
                   │                └─ may call web_search (Parallel) before answering
                   └─ also detects turns:
                      turn.end    the user finished a sentence → start a reply
                      turn.start  the user began speaking     → cancel the reply in progress
```

- **Replying.** When a turn ends, its transcript goes to the model. The model's text is pushed to text-to-speech as it streams, and the audio is forwarded to the client as it is produced. Nothing is held back on the way: over ten real turns the first audio came 0.11 s after the model's first word, and the model's first word (1.1 s) was nearly all of the wait.
- **The speech connection is opened early.** One is opened when the call starts and again each time the user starts speaking, so it is there before the reply. Opening takes about 0.12 s and used to run while the model was thinking, so this only matters when opening is slow.
- **Who speaks first.** The user, unless the gem or the app asks otherwise: a gem that speaks first, or `first` set to `assistant` in `call.start`, has the gem greet the user as soon as the call opens.
- **Interrupting.** When the user starts speaking, the server cancels the model call and the speech stream, then sends an `interrupt` event. The client must stop playback and drop audio it has not played yet: speech is generated several times faster than it plays, so the client always holds some.
- **Searching.** With `PARALLEL_API_KEY` set, the model gets a `web_search` tool, unless the gem's `tools` leave it out. Without the key the tool is not offered and everything else works. One reply may search as many times as the model needs: there is no limit.

### Which model answers

A conversation is held with one model from start to end, chosen when it opens:

| Model | Provider | Thinks before it speaks | Needs |
| --- | --- | --- | --- |
| `6 Luna` (the default) | OpenAI | no | `OPENAI_API_KEY` |
| `6.1 Terra` | OpenAI | no | `OPENAI_API_KEY` |
| `6.1 Sol` | OpenAI | a little: it cannot be turned off | `OPENAI_API_KEY` |
| `Haiku 5.5` | Anthropic | no | `ANTHROPIC_API_KEY` |
| `Opus 5.5` | Anthropic | a little: it cannot be turned off | `ANTHROPIC_API_KEY` |
| `Sonnet 5.5` | Anthropic | no | `ANTHROPIC_API_KEY` |

A conversation is always held with the gem's model. `VOICE_MODEL` sets the model for a gem that names none. `GET /v1/voice/models` lists them, with whether each one's key is set. A call with a gem whose model's provider has no key gets `call.refused`, with the reason in words.

OpenAI can also be reached through a proxy that speaks its API, such as [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) signed in with a ChatGPT account, so the ChatGPT plan pays instead of the API key. Set `OPENAI_PROXY_URL` (for CLIProxyAPI on this Mac, `http://127.0.0.1:8317/v1`) and `OPENAI_PROXY_KEY` (one of the proxy's own keys). With the proxy and `OPENAI_API_KEY` both set, the user picks one in the app: `GET /v1/providers/openai` says which is in use and which are set up, and `PUT` with `{"via": "api"}` or `{"via": "proxy"}` switches it from the next request on, for calls in progress too. The choice is kept across restarts; until one is made, the API key is used. A way that is not set up gets 409. The plan's models must include the gem's, and its usage limits apply to calls.

The proxy's ChatGPT account can be changed from the app too, when `OPENAI_PROXY_MANAGEMENT_KEY` is set to CLIProxyAPI's management key (`remote-management.secret-key` in its config). `GET /v1/providers/openai` then lists the proxy's accounts, each with its plan and whether the proxy can still use it. `POST /v1/providers/openai/sign-in` starts a sign-in and gives the address to open in the browser, which comes back to the proxy, not to this backend. `GET /v1/providers/openai/sign-in/{state}` says `wait`, `ok` or `error`. When it is `ok`, the account just signed in replaces the proxy's other ChatGPT logins, so it is the one every request uses. No login ever reaches this backend's database or the app.

Everything below is the same for either provider. The conversation keeps one history, and only the last step differs: OpenAI is sent it as it stands, and for Anthropic it is translated into alternating user and assistant messages on every request. These things differ on an Anthropic model:

- **Thinking is fixed for the conversation.** It is off on Haiku 5.5, on Sonnet 5.5 it is set to the nearest that model allows, and Opus 5.5 is asked for as little as it will do. What a model writes as thinking is never sent back to it, because the history is cut down to what the user heard. The switch to a little thinking while a search is pending (rule 2 below) is not made, and is not needed: asked "hello?" over a running search, Haiku 5.5 and Opus 5.5 each started no second search in 20 runs of 20.
- **Notes from the server travel in a user message**, wrapped in `<note>` tags that the instructions explain. The Messages API has no place for them where they fall, which is after a reply of the model's own.
- **Caching is asked for on every request.** Conversations shorter than the provider's minimum are not cached at all.

Fable 5.2 is not offered: its thinking cannot be turned off, which is a wait before every reply. Opus 5.5 has the same limit and is offered anyway, for comparing.

One smaller difference: two searches made in one round are sent back to an Anthropic model as two rounds, one after the other.

The Anthropic models are reached through the Claude API directly, with a key from platform.claude.com, never through a cloud provider. They have been run live: every request shape the conversation makes is accepted by Haiku 5.5, Opus 5.5 and Sonnet 5.5, and the real-model check below passes on Haiku 5.5 and Opus 5.5.

### What the model remembers

The history is what the model is sent on every request. A call starts from the gem's stored conversation and adds its turns to it as they are settled (see [One conversation](#one-conversation)). Nothing is stored at the provider. The history follows three rules, the same in a call and in writing.

1. **It holds what the user heard, not what was generated.** A reply joins the history only once it has been heard in full or cut off. A reply that is cut off is stored as the words that were heard, ending in "…", and the rest is never sent back. How much was heard comes from Cartesia's word timings and a server-side copy of the client's playback schedule, shifted by the sound-device delay the client reports (about 40 ms on laptop speakers, a few tenths of a second on Bluetooth). In live runs the stored text was within two words of the audio really played, and never past it. Nothing is added about the unheard part, however long it was: the "…" and the model's instructions say that the user did not hear the rest.
2. **Every tool call keeps a valid result.** An interruption does not stop a tool call, in a call or in chat, and the same code does it in both ([`app/entities/late_results.py`](app/entities/late_results.py)). Its call gets a placeholder result and the call carries on. What the user said is answered first. Then, at the next quiet moment, the real result is added as a call written by the server (a stub that names the original call) followed by the result, and the model tells the user about it in a reply of its own, without being asked. That holds even when the result came back while the user was still speaking: put in with their words, the model answered them and never mentioned it. A quiet moment is when the reply in progress has finished playing, so a result that lands during a long reply waits for it. If a noise cuts the reply about the result off before a word of it is heard, the same request is made again. If the user speaks over it instead, they are answered, and telling them of the result is left to the model: its instructions ask it to answer first and then tell them. The model can stop a running call with `cancel_tool_call`. A cancel made in the instant the call finishes still drops its result, since the model was told "Cancelled.". While a search has no result in the history yet, the model is asked to think a little before it speaks; otherwise it does not think at all. The first word of those replies came after 1.4 s at the median and 2.6 s for the slowest tenth, against 0.9 s and 1.1 s without thinking. Asked without thinking, it started the same search a second time in a third to a half of measured runs where the user only said "hello?"; with it, about one run in twenty. The switch also costs one prompt-cache miss, on the first request made with thinking on; the request after the switch back was served from the cache again.
3. **Nothing the model has been sent is changed afterwards**, with one exception: the user's open turn, always the last item, can gain a line. Each request starts with the whole of the one before it, and the instructions and tool list are identical every time, so a prompt cache stays valid. One consequence: what the model said before a search is kept in full even if the user cut it off part-way, because the model was already sent it. Nothing is cleared: every search result stays whole, as in chat.

The user's turn is put together before it reaches the model. A turn stays open until some of the reply to it has been heard, and words that arrive before then join it on a new line, because speech recognition sometimes splits one sentence in two. In live runs that was the first two to three seconds after the first half ended.

A one or two word fragment in a writing system the user has not spoken in during the conversation, starting within three seconds of the end of their sentence, is dropped as noise. So is one the recognizer has run on to the end of the sentence itself. This only works when the recognizer writes the noise in another script: it sometimes writes the same sound in Latin letters, and then nothing marks it as noise. A turn with no usable words puts back whatever it interrupted: an unanswered question is answered again, and a reply that was being heard is resumed. For the second, the history gets one line saying that the sound had no words in it, and the model's instructions tell it to carry on from where it stopped. The limits on the filter matter, because a real word dropped as noise means the user is talked over.

The conversation is in [`app/entities/voice_ent.py`](app/entities/voice_ent.py). Speaking, and working out what was heard, is in [`app/entities/voice_speech.py`](app/entities/voice_speech.py). What the model is told is put together in [`app/entities/instructions.py`](app/entities/instructions.py). What differs between the providers is in [`app/entities/model.py`](app/entities/model.py), and the web search tool is in [`app/entities/tools.py`](app/entities/tools.py). Neither is particular to a call.

### The loop, and the surface a reply is delivered on

Making a reply is not particular to a call either. The loop in [`app/entities/loop.py`](app/entities/loop.py) runs one: a model round, then the tool calls it made, then their results back to the model in the next round, until a round only says something. Every gem, however it is reached, is to run this one loop. What differs is its config and the surface its reply is delivered on. A surface takes the reply's text and can say how much of it got through by a given moment. In a call the surface is speech, and what got through is what was heard. The history holds what was delivered, by the rules above, on any surface.

[`app/entities/written_ent.py`](app/entities/written_ent.py) is a second surface, writing. One message in, its reply streamed out as text, one reply at a time; or a reply with no new message, written from the history as it stands. It is given the history to start from and hands back what it added. What was delivered is what the reader was handed: if the reply is cut or the reader stops part-way, the history keeps that text ending in "…". A tool still running is not stopped: as in a call, it gets the placeholder as its result, and is held by the conversation's `LateResults` until whoever holds the conversation tells its result. Chat is built on it.

## Chat

A gem can be written to as well as called. The app keeps one WebSocket open to `/v1/link`, and chat goes over it, as do calls (see [Calls](#calls)); the frames are in [`docs/chat-contract.md`](docs/chat-contract.md), which is the contract with the app. In short: the app sends `chat.send` with a gem's id and the text, and gets back `chat.text` and `chat.tool` frames as the reply is written, then one of `chat.done`, `chat.cut` or `chat.error`. A search that carries on after a cut is told later, in a reply that starts with `chat.result`. A frame that cannot be read gets `error`, and the socket stays open.

**The database is the truth, not the connection.** Every message is answered from what is stored: the gem's config and the Darbar text are loaded as they are now, its conversation so far is loaded (a call's turns included), the instructions are built, one reply is made by the loop, and then what the user wrote and what was delivered of the reply are added to the stored conversation, before the reply's last frame is sent. Little about a gem's conversation is kept in memory between messages: the reply in progress, the searches still running, and messages no reply has got through to yet. So a changed gem applies from the next message, two windows on one gem cannot drift apart, and a dropped connection loses nothing: a socket that closes cuts its replies, and they are stored as cut.

The stored conversation is the history the model is sent, item by item, only ever added to, with each item's place and when it was added. It follows the same rules as a call's: it holds what was delivered, every tool call has a result, and each request starts with the whole of the one before it. A reply cut while it was being written is stored as what was sent, ending in "…". Nothing is made up to mark a cut: a reply cut before any of it was sent leaves no reply, as in a call, and one cut while a tool ran keeps the text before it whole.

**A message the reply never reached joins the next one**, as words do in a call. A message whose reply was cut or failed before any of it was delivered stays open: the next message to the gem joins it on a new line, and they are stored as one user message with the reply that gets through. An open message is held in memory only, so it is lost if the process stops before a reply gets through. The rule is one, for a call and for writing, in [`app/entities/history.py`](app/entities/history.py).

**A search carries on after a cut, as in a call.** Its call is stored with the call's placeholder as its result, and the new message is answered first. When the search finishes, at the next moment no reply is being written to that gem, a stub call and the real result are added to the stored conversation, in the call's shape, and the gem tells the user what it found in a reply of its own, with no new message. That reply goes to the socket that sent the latest message to the gem, after a `chat.result` frame; if that socket is gone, the result is only stored, and the gem's instructions tell it to bring the result up when it next answers. A result that comes in while a reply is being written waits for it to be stored, and several waiting are told in one reply. That reply is a turn like any other: a new message cuts it. The gem can stop a running search with `cancel_tool_call`, and while a search has no result in the conversation yet it is asked to think a little first. Which stub delivered which call is read from the stub's arguments. A placeholder, and the answer of a `cancel_tool_call` that stopped a call, are stored with a `mark` that says so, and are found by it, never by their wording. The calls themselves run in the process's memory, so a restart loses them; before the next reply, a placeholder whose call is gone gets a stub with a result saying the call was lost and will not arrive (its wording is not settled). It is all in [`app/entities/chat_ent.py`](app/entities/chat_ent.py), with the late-result code it shares with a call in [`app/entities/late_results.py`](app/entities/late_results.py).

`GET /v1/gems/{gem_id}/chat` returns the gem's conversation for showing on screen, a call's turns included: `user`, `gem` and `tool` messages, oldest first, each with `at`. Stub calls, `cancel_tool_call` calls and our own lines are not shown; a reply about a late result is a `gem` message. It does not say which messages were spoken. Deleting a gem deletes its conversation, and a late result of a deleted gem is dropped.

Two first rules, to be tried out rather than settled:

- **The whole stored conversation is sent to the model every time.** Nothing is trimmed or summed up, and old search results are not cleared, as in a call. The context therefore grows with every message, and quickly when searches are used. A message is at most 20,000 characters, so one stray paste cannot ride along in every request after it.
- **A new message cuts the reply still being written to that gem**, like speaking over someone in a call. The reply is stored as what was sent plus "…", its socket gets `chat.cut`, and then the new message is answered. This holds across every socket of the process: one reply at a time for each gem.

While the gem is in a call, a message to it gets `chat.error` with "the gem is in a call", and nothing is stored.

A gem in writing is offered `web_search` when its `tools` name it and the search key is set, and with it `cancel_tool_call`, as in a call.

## Try it

```bash
docker compose up -d                      # MongoDB and Redis
uv sync --extra dev
cp .env.example .env                      # then set CARTESIA_API_KEY and the model's key (OPENAI_API_KEY by default)
uv run uvicorn app.main:app --reload
```

The desktop app is the client: it keeps one socket open to `/v1/link` and holds its chats and calls over it (see [`AGENTS.md`](AGENTS.md)).

## Calls

A call goes over the standing socket, `WS /v1/link`, which carries chat too. The app sends `call.start`, mic audio as binary frames, `call.playback` and `call.end`. It gets `call.ready`, speech as binary frames, the call's events with `call.` put before their type (a reply comes as words timed to its audio, not as text), and one last frame: `call.ended`, `call.refused` or `call.failed`. The socket stays open through all of it.

A call is always with a gem (see [Gems](#gems)): `call.start` names it, and may say who speaks first. The gem's model answers. A `first` that is sent wins over the gem's for that call, and one that is left out leaves the gem's. A `call.start` with no gem or a value that is not offered, a gem that is not there or an id that is not an id, a gem already in a call, or a gem whose model's provider has no key, gets `call.refused` with the reason in words. A gem whose stored config cannot be read, as when a newer cortex wrote it, gets `call.failed`, and so does a call whose conversation cannot be read or stored. Opening a call, and storing what it adds, is in [`app/entities/call_ent.py`](app/entities/call_ent.py).

## One conversation

A gem has one conversation, whether the user calls it or writes to it. A call starts from the gem's conversation as it is stored, as a written reply does, and a written reply after a call is made from everything the call added. The only difference between the two is the speech: a call has speech-to-text and text-to-speech around the same loop, and writing has none.

- **A call stores what it settles, as it goes.** The user's turns, the gem's replies as they were heard, tool calls and their results, late results and our own lines are stored in order after what was there, by the same code chat stores with. The user's open turn and a reply that can still be cut are never stored. Everything settled is stored by the time the call's last frame is sent; if it cannot be, the call fails with `call.failed` and "the conversation could not be stored". The Darbar text is read once when a call opens, and for every message in writing.
- **A line says when a call starts and when it ends.** Before the first turn of every call: "The user has called you. From here on the conversation is spoken." Before the first written turn after a call: "The call has ended. From here on the conversation is written." Nothing is written when a call ends: the line is written when the next turn needs it. A line is stored with the first item that follows it, so a call in which nothing was said stores nothing. A call straight after another call is marked by its own opening line.
- **Every stored item says its mode**, `call` or `chat`. Items stored before calls were kept have none, and are read as written. The collection keeps its name, `ChatItems`.
- **One writer at a time.** A call holds the gem from when speech is connected until what it added is stored. Meanwhile a message to the gem gets `chat.error` ("the gem is in a call"), and another `call.start` for it, from any socket, gets `call.refused` with the same words. A `call.start` while a reply is being written to the gem cuts that reply, as a new message does, and the call opens once it is stored. A message whose reply never got through is stored as it is, with no reply, before the call opens: the call follows it rather than drop it.
- **One set of late results.** A search left running by a written reply whose result comes in during a call is told in the call. One still running when a call ends carries on, and its result is told in writing, to the client of the latest message, or only stored if there is none. A late result is never written about to a gem in a call.
- **Calls lost to a restart are closed** with the lost stub before a call's first reply, as before a written one.

What is kept in memory of each gem's conversation (the writer, the late results, a written turn left open) is in [`app/entities/conversation_ent.py`](app/entities/conversation_ent.py).

The frames, what each of the call's events means, the rules a call keeps to, and how a client must play its audio are in [`docs/chat-contract.md`](docs/chat-contract.md).

`GET /v1/voice/models` returns one object per model: `name` (what to send), `provider`, `label` (what to show), `available` (its key is set), `default` (used when none is named) and `listed` (whether a picker should offer it).

## Gems

A gem is one of the user's assistants. Each has one config, kept in the `Gems` collection but treated like a config file. It is the one home of a gem: what a call or a chat acts on, and what the app shows of it:

| Field | Meaning |
| --- | --- |
| `version` | the version of the config's shape, 3 today. Every stored config carries it |
| `name` | what the gem is called: required, trimmed, at most 100 characters |
| `description` | one line on what the gem is for, shown under its name. The model is not told it. May be empty; at most 200 characters |
| `image` | the gem's picture, as one value the backend does not read: today the app's built-in face and colour, later perhaps the address of a picture. Null for none; at most 500 characters |
| `instructions` | plain text saying who the gem is and how it behaves. A gem made with none, or blank ones, is given the default instructions: how to talk in a call and how to write. After that they are stored as they are sent, so blank means the user removed them. At most 20,000 characters |
| `model` | a `name` from `GET /v1/voice/models`, or null for the configured default. A stored name that is no longer offered is read as null |
| `speaks_first` | whether the gem greets the user when a call connects. Default false |
| `tools` | the names of the tools it may use, no name twice. Default `["web_search"]`, the only tool there is today. Any other name is refused when a gem is made or changed |
| `voice` | the voice its replies are spoken in, as the app names it. Null for none chosen; at most 100 characters. Kept, but a call does not use it yet |
| `speech_speed` | how fast it is spoken, 0.5 to 2.0. Default 1.0. Kept, but a call does not use it yet |

Something new (skills, say) is added by raising the version and writing one step that brings older configs up to the new shape. Version 2 added `description`, `image`, `voice` and `speech_speed`; a version 1 config reads with nothing chosen for them. Version 3 made the rules for a call and for writing part of a gem's instructions, where the code used to add them: an older config's instructions read as its own text, a blank line, then the default instructions (or the defaults alone if it had none). An older config is stored in the new shape the next time it is changed, whatever the change (`upgrade_gem_document` in [`app/entities/gem_ent.py`](app/entities/gem_ent.py)). A config from a newer version than the code knows is refused. One with no version is read as version 1, the first shape. A stored config that names a tool which has since gone still loads: a call just does not offer that tool.

Every call is held with a gem, named by its id as `gem` in `call.start`. Its model answers, it greets the user if it speaks first, and it is offered web search only if its `tools` name `web_search` and the search key is set. Cortex is the name of the project, which holds the gems, not of an assistant.

What a gem is told is one text, the same in a call and in writing, whichever tools it is offered. It is put together in [`app/entities/instructions.py`](app/entities/instructions.py), from what changes least to what changes most: the Darbar text, then "You are {name}." and the gem's own instructions, then what the model itself needs (the line about `<note>` tags on an Anthropic model), then the date. The order keeps the start of every request the same for as long as possible, which is what a prompt cache needs. The Darbar text is the environment, the same for every gem: what a call and writing are, what a reply ending in "…" means, and what a late tool result is. The gem's own instructions say how it talks in a call and how it writes, by default (see `instructions` above). Whether the conversation is spoken or written is said by a line in the `developer` role where it changes: `The user has called you. From here on the conversation is spoken.` before a call's first turn after writing, and `The call has ended. From here on the conversation is written.` before the first written turn after a call (see [One conversation](#one-conversation)).

The Darbar text is a setting the user can change. Until they do, it is a built-in text (`DEFAULT_DARBAR`): a draft the owner has yet to read. A text the user writes takes its place whole, so a gem told an empty or custom one is not told what the built-in one says. It is kept as one document in the `Darbar` collection, at most 4,000 characters, and an empty one means gems are told nothing there. A call reads it once, when it opens, and keeps that text to its end, so a change reaches the next call, never one already open. A written reply reads it each time, so a change applies from the next message. It is in [`app/entities/darbar_ent.py`](app/entities/darbar_ent.py).

| Route | Does |
| --- | --- |
| `POST /v1/gems` | makes a gem (201) |
| `GET /v1/gems` | lists them in the order they were made. Changing a gem does not move it |
| `GET /v1/gems/{gem_id}` | one gem |
| `PATCH /v1/gems/{gem_id}` | changes only the fields sent. Only `model`, `image` and `voice` may be null |
| `DELETE /v1/gems/{gem_id}` | deletes it (204), and its conversation |
| `GET /v1/gems/{gem_id}/chat` | its conversation so far, calls included (see [Chat](#chat)) |
| `GET /v1/darbar` | the Darbar text: `{"text": "...", "is_default": true}`. `is_default` says whether it is the built-in text |
| `PUT /v1/darbar` | sets it. Body `{"text": "..."}`, at most 4,000 characters; may be empty. Answers like `GET` |
| `DELETE /v1/darbar` | goes back to the built-in text. Answers like `GET` |

Ids go out as strings, and a missing or malformed id is a 404. There is no sign-in on any of these: this backend has one user for now. How the app uses them is in [`docs/chat-contract.md`](docs/chat-contract.md).

## Layout

Imports only go one way: `config ← core ← adapters ← entities ← routers ← main`.

```text
app/
  adapters/    one file per outside service; the only place an SDK is called
               openai_adp · anthropic_adp · cartesia_stt_adp · cartesia_tts_adp · parallel_adp · docdb_adp · redis_adp
  entities/    the logic: loop (making one reply, on any surface), voice_ent and voice_speech (the cascade), written_ent (a written conversation), late_results (tool calls that outlive their reply, held until their results are told: one copy for a call and for chat), history (the rules for what joins a history: the user's turn, the mode lines, what is kept beside an item), conversation_ent (what is kept in memory of each gem's one conversation, and loading and adding to it), conversation_store (each gem's conversation in the database), chat_ent (written chat with the gems, made from what is stored), call_ent (opening a call, and storing what it adds), model (either provider's model, for any conversation), instructions (what the model is told, in layers), tools (the tools a model may call, each a small object, and which a gem can be given), gem_ent (a gem's config), darbar_ent (the Darbar text every gem is told first), user_ent
  routers/     HTTP and WebSocket endpoints: voice (the model list), gems, darbar, link (the standing socket, which carries chat and calls), user
```

Also still present from earlier experiments: user registration and login (`/v1/users`), kept because a login will be needed.

## Development

```bash
uv run pytest
uv run ruff check app tests
uv run pyright
```

To check the model and the pipeline together without any audio, with the real OpenAI model and the real web search:

```bash
uv run python -m scripts.voice_pipeline_check              # the default model, 6 Luna
uv run python -m scripts.voice_pipeline_check "Opus 5.5"   # or any other model's name
```

It fakes speech so each scenario can say how much of a reply was heard, and prints PASS or CHECK for 24 behaviours: context, search, cut replies, late search results, cancelling, noise, merged turns, many searches, another language, service failure and spoken style. A scenario that does not finish is shown as CHECK and the rest still run.

Configuration is read from the environment by `app/config.py`. `.env.example` lists every setting. Never commit credentials.
