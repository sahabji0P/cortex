# Cortex

Cortex is a personal experimentation backend. Its current experiment is a spoken conversation built as a cascade: speech-to-text, then a model, then text-to-speech, with interruption handling and a web search tool.

## The voice cascade

```text
mic audio ──▶ speech-to-text ──▶ model ──▶ text-to-speech ──▶ speech audio
              Cartesia ink-2     OpenAI    Cartesia Sonic
                   │                │
                   │                └─ may call web_search (Parallel) before answering
                   └─ also detects turns:
                      turn.end    the user finished a sentence → start a reply
                      turn.start  the user began speaking     → cancel the reply in progress
```

- **Replying.** When a turn ends, its transcript goes to the model. The model's text is pushed to text-to-speech as it streams, and the audio is forwarded to the client as it is produced.
- **Interrupting.** When the user starts speaking, the server cancels the model call and the speech stream, then sends an `interrupt` event. The client must stop playback and drop audio it has not played yet: speech is generated several times faster than it plays, so the client always holds some.
- **Searching.** With `PARALLEL_API_KEY` set, the model gets a `web_search` tool. Without it the tool is not offered and everything else works.

### What the model remembers

The history is what the model is sent on every call. It lives in memory for the life of the connection, nothing is stored at OpenAI, and it follows three rules.

1. **It holds what the user heard, not what was generated.** A reply joins the history only once it has been heard in full or cut off. A reply that is cut off is stored as the words that were heard, ending in "…", and the rest is never sent back. How much was heard comes from Cartesia's word timings and a server-side copy of the client's playback schedule. In live runs the stored text was within two words of the audio really played, and never past it.
2. **Every tool call keeps a valid result.** An interruption does not stop a search. Its call gets a placeholder result and the search carries on. When it finishes, the real result is added as a call written by the server (a stub that names the original call) followed by the result, and the model answers it at the next quiet moment without waiting for the user. The model can stop a running search with `cancel_tool_call`.
3. **Nothing the model has been sent is changed afterwards.** Each request starts with the whole of the one before it, and the instructions and tool list are identical every time, so a prompt cache stays valid. Two consequences: what the model said before a search is kept in full even if the user cut it off part-way, because the model was already sent it; and the one item that can still change is the user's open turn, always the last, which can gain a line.

The user's turn is put together before it reaches the model. A turn stays open until some of the reply to it has been heard, and words that arrive before then join it on a new line, because speech recognition sometimes splits one sentence in two. A one or two word fragment in a different script, right after a finished sentence, is dropped as noise. A turn with no usable words puts back whatever it interrupted.

The conversation is in [`app/entities/voice_ent.py`](app/entities/voice_ent.py). Speaking, and working out what was heard, is in [`app/entities/voice_speech.py`](app/entities/voice_speech.py).

## Try it

```bash
docker compose up -d                      # MongoDB and Redis
uv sync --extra dev
cp .env.example .env                      # then set OPENAI_API_KEY and CARTESIA_API_KEY
uv run uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000/v1/voice/test>, press **Start talking**, and allow the mic. This is a one-file test page, not a product UI. Use headphones if Cortex cuts itself off, which means the mic is picking up the speakers.

## The WebSocket contract

`WS /v1/voice/converse`

| Direction | Frame | Content |
| --- | --- | --- |
| client → server | binary | mic audio: raw 16-bit mono PCM at 16 kHz, about 100 ms per frame |
| server → client | binary | speech audio: raw 16-bit mono PCM at 24 kHz |
| server → client | text | JSON `{"type": ..., "text": ...}` |

| Event type | Meaning |
| --- | --- |
| `user.partial` | the user's words so far, while they are still speaking |
| `user.final` | the user's finished sentence; a reply follows |
| `assistant.delta` | the next piece of the reply's text |
| `assistant.done` | all of the reply's audio has been sent; `text` holds the full reply |
| `tool.call` | the assistant is searching the web; `text` holds the query |
| `interrupt` | the user started speaking: stop playback and drop queued audio. If that cut a reply short, `text` holds the part that was heard |
| `error` | something failed; `text` holds the message |

## Layout

Imports only go one way: `config ← core ← adapters ← entities ← routers ← main`.

```text
app/
  adapters/    one file per outside service; the only place an SDK is called
               openai_adp · cartesia_stt_adp · cartesia_tts_adp · parallel_adp · docdb_adp · redis_adp
  entities/    the logic: voice_ent and voice_speech (the cascade), stt_ent, conversation_ent, user_ent
  routers/     HTTP and WebSocket endpoints: voice, cortex, user
```

Also still present from earlier experiments: user registration and login (`/v1/users`), projects and sessions (`/v1/cortex`), a transcription-only WebSocket (`/v1/cortex/transcribe`), and a Claude Agent SDK pipeline that no route uses.

## Development

```bash
uv run pytest
uv run ruff check app tests
uv run pyright
```

Configuration is read from the environment by `app/config.py`. `.env.example` lists every setting. Never commit credentials.
