<p align="center">
  <img src="assets/logo.png" alt="Cortex" width="120">
</p>

<h1 align="center">Cortex</h1>

<p align="center">
  The backend for a set of personal assistants, called gems, that you can call or write to.
</p>

---

Cortex holds the user's gems. A gem has a name, instructions, a model and tools. You can
talk to it in a live voice call or write to it in a chat. Either way it is **one
conversation**, stored, and it carries on from where it left off. The desktop app,
[cortex-desktop](https://github.com/sahabji0P/cortex-desktop), is the client.

It is a personal project and an experiment. It runs on your own Mac, for one user.

## Features

- **Voice calls as a cascade.** Speech-to-text, then a model, then text-to-speech. You
  can interrupt at any time, and the gem remembers only what you actually heard.
- **Written chat** with the same gem, over the same socket, from the same stored
  conversation.
- **Observability built in.** Every gem has a log of what it did: each turn,
  each request with exactly what it was sent, its tokens, cost and timing, each tool
  call from start to end, and each change of model, instructions or tools. Read it over
  HTTP or watch it live. The app draws it as the gem's trajectory.
- **Six models from two providers.** OpenAI (6 Luna, 6.1 Terra, 6.1 Sol) and Anthropic
  (Haiku 5.5, Sonnet 5.5, Opus 5.5). OpenAI can also be reached through a ChatGPT login
  by a proxy.
- **Tools.** Web search, and the tools of any [MCP](https://modelcontextprotocol.io)
  server you connect, with browser sign-in (OAuth) and per-gem permissions. A tool call
  carries on in the background when a reply is cut, and its result is told later.

## How it works

```text
mic audio ──▶ speech-to-text ──▶ model ──▶ text-to-speech ──▶ speech audio
              Cartesia ink-2     OpenAI or  Cartesia Sonic
                   │             Anthropic
                   │                │
                   │                └─ may call tools (web search, MCP) before answering
                   └─ also detects turns:
                      turn.end    the user finished a sentence → start a reply
                      turn.start  the user began speaking     → cut the reply in progress
```

The app keeps one WebSocket open to `/v1/link`. Calls and written chat both go over it.
In writing there is no speech: the same loop runs, and the reply is streamed as text.
Plain HTTP routes save and load gems, connections and settings.

Three rules hold for what a gem remembers, in a call and in writing:

1. **It holds what was delivered**: what the user heard, or was sent, not what the
   model generated. A cut reply is kept as far as it got, ending in "…".
2. **Every tool call keeps a valid result**, even when the reply that made it is cut.
3. **Nothing the model was sent is changed afterwards**, so a prompt cache stays valid.

[Architecture](docs/architecture.md) explains these in full.

## Observability

Every gem has a log: everything that happens with it, in one list, in the order it
happened. It is not a trace kept beside the conversation. The gem's conversation *is*
the five kinds of entry it remembers, and everything else that happened is written in
between. So for any request you can see exactly what the model was sent, what it
produced, what the user actually got, and what it cost.

A message that starts a web search, is cut off by a second message, and has its search
result told later leaves this (trimmed from the code's own output):

```text
seq turn kind               of note
  0  -   settings.changed   model 6 Luna via api; 3 layers of instructions; 2 tools
  3  1   user.said          "weather?"
  4  1   request.sent       request 1, sent 2 items
  5  1   request.done       "Let me look." and web_search {"query": "Pune weather"}
  6  1   tool.started       call_1, web_search
  7  1   tool.backgrounded  call_1: the reply was cut, the search carries on
 11  1   turn.closed        cut
 13  2   user.said          "and the time?"
 14  2   request.sent       request 2, sent 6 items, think true: a search is still running
 18  1   tool.done          call_1, done in the background, with its result
 21  3   turn.opened        by background_result
 24  3   gem.said           "It is sunny in Pune."
```

- **Exactly what was sent**: `GET /v1/gems/{id}/log/{seq}/sent` rebuilds any request,
  item by item, with its instructions and tools.
- **Tokens and cost** for every request, from a price table kept in the repo. Unknown is
  null, never zero.
- **Live**: send `log.watch` on the socket and each entry arrives as it is written.
- **Free for the reply**: writing the log never holds a reply up and never fails one,
  and the model is sent the same with it or without it.
- **Not yet in it**: speech-to-text, speech and search, with their cost, and the retries
  a provider's library makes on its own.

[Observability](docs/observability.md) has the whole trace, the promises the log keeps,
and how to read it.

## Getting started

You need [uv](https://docs.astral.sh/uv/), Docker (for MongoDB and Redis), a
[Cartesia](https://cartesia.ai) key for speech, and a key for the model's provider.

```bash
docker compose up -d          # MongoDB and Redis
uv sync --extra dev
cp .env.example .env          # then set CARTESIA_API_KEY and OPENAI_API_KEY
uv run uvicorn app.main:app --reload
```

To use an Anthropic model by default instead, set `ANTHROPIC_API_KEY` and `VOICE_MODEL`
(say, `Haiku 5.5`). The backend listens on `http://127.0.0.1:8000`. `GET /health` answers when it is up.
Then run the [desktop app](https://github.com/sahabji0P/cortex-desktop) against it.

## Configuration

Settings are read from the environment, or from `.env`, by
[`app/config.py`](app/config.py). [`.env.example`](.env.example) lists them all.

| Setting | Needed for | Default |
| --- | --- | --- |
| `CARTESIA_API_KEY` | calls: speech-to-text and text-to-speech | none |
| `OPENAI_API_KEY` | the OpenAI models | none |
| `ANTHROPIC_API_KEY` | the Anthropic models (a Claude API key, from platform.claude.com) | none |
| `PARALLEL_API_KEY` | the `web_search` tool. Without it the tool is not offered | none |
| `VOICE_MODEL` | the model of a gem that names none | `6 Luna` |
| `OPENAI_PROXY_URL`, `OPENAI_PROXY_KEY` | OpenAI through a proxy on a ChatGPT login ([Models](docs/models.md#openai-through-a-chatgpt-login)) | none |
| `OPENAI_PROXY_MANAGEMENT_KEY` | changing the proxy's ChatGPT account from the app | none |
| `BACKEND_URL` | where the browser comes back to after a connection's sign-in | `http://127.0.0.1:8000` |
| `MONGODB_URL`, `MONGODB_DB_NAME` | the database | `mongodb://localhost:27017`, `cortex` |
| `REDIS_URL` | Redis, connected at start-up. Only the user login (`/v1/users`) writes to it | `redis://localhost:6379` |
| `ENVIRONMENT`, `DEBUG` | `local`, `staging` or `production`; `DEBUG` opens the API to any web origin | `local`, `true` |
| `SECRET_KEY`, `ACCESS_TOKEN_EXPIRE_MINUTES`, `JWT_ALGORITHM` | the user login (`/v1/users`) | see `.env.example` |

Never commit credentials.

## API at a glance

| Route | What it is |
| --- | --- |
| `WS /v1/link` | The standing socket: written chat, calls and their audio, and the log as it is written |
| `/v1/gems` | The gems and their configs, a gem's conversation (`/chat`), its log (`/log`), and a reset (`/reset`) |
| `/v1/darbar` | The Darbar text, which every gem is told first |
| `/v1/connections` | The MCP tool servers, who may use them, and their sign-ins |
| `/v1/providers/openai` | How OpenAI is reached: the API key or the proxy, and the proxy's accounts |
| `GET /v1/voice/models` | The models a gem can use, and whether each one's key is set |
| `/v1/users` | Register and log in. The app does not use it |
| `GET /health` | Whether the backend is up |

Every frame and field the app relies on is in [the contract](docs/chat-contract.md).
FastAPI also serves its own schema at `/docs` while the backend runs.

## Documentation

| Document | What it covers |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | How the code is layered, the loop, one conversation, and what a gem remembers |
| [docs/observability.md](docs/observability.md) | A gem's log: what it records, what it answers, what it promises, and how to read or watch it |
| [docs/gems.md](docs/gems.md) | A gem's config, its versions, and what a gem is told (the Darbar text and its instructions) |
| [docs/models.md](docs/models.md) | The models, how the two providers differ, the ChatGPT proxy, and cost |
| [docs/connections.md](docs/connections.md) | MCP tool servers: permissions, the copy of their tools, deferred tools, and browser sign-in |
| [docs/chat-contract.md](docs/chat-contract.md) | The contract with the app: every route, frame and field, and the rules for a call's audio |
| [docs/voice-spec.md](docs/voice-spec.md) | What a call does and what the model is sent, rule by rule, with what was measured live and the known gaps. [voice-context.excalidraw](docs/voice-context.excalidraw) is the same as a picture |
| [docs/darbar-notes.md](docs/darbar-notes.md) | The plan: the project's words, what is decided, what comes next, what is put off |
| [AGENTS.md](AGENTS.md) | Rules for working on this repo: what the app mirrors, which doc owns what, how to commit |

## Project layout

Imports go one way only: `config ← core ← adapters ← entities ← routers ← main`.

```text
app/
  config.py     settings, read from the environment
  core/         errors, password and token helpers, times
  adapters/     one file per outside service, and the only place an SDK is called
  entities/     the logic: the loop, calls, chat, gems, connections, the log
  routers/      HTTP and WebSocket endpoints, one folder each
  main.py       the FastAPI app
tests/          the test suite, with stand-ins for every outside service
scripts/        checks run by hand against real models
docs/           the documents above
```

[Architecture](docs/architecture.md#where-the-code-is) says what each file holds.

## Development

```bash
uv run pytest                 # the test suite: no keys or services needed
uv run ruff check app tests   # lint
uv run pyright                # types
```

Two checks run against real models and cost money. They need the model's key in `.env`:

```bash
uv run python -m scripts.voice_pipeline_check "6 Luna"   # a call's model and pipeline, with real web search (needs PARALLEL_API_KEY)
uv run python -m scripts.deferred_tools_check "6 Luna"   # deferred tools: the search, the loading, and the prompt cache
```

The first fakes speech, so each scenario can say how much of a reply was heard, and
prints PASS or CHECK for each behaviour: context, search, cut replies, late results,
cancelling, noise, a sentence split in two, another language, failures and spoken style.

## Contributing

Read [AGENTS.md](AGENTS.md) first. In short:

- The app mirrors this backend's routes and frames by hand. A change to a route, a frame
  or a field is a change to the contract: update [the contract](docs/chat-contract.md)
  and say so in the pull request.
- Commit in small steps, from the bottom layer up. Each commit passes `uv run pytest`
  and `uv run ruff check app tests` on its own.
- The repository is public: no transcripts, records or real instructions in commits.
