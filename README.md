# Cortex

Cortex is a personal experimentation backend. Its current experiment is a spoken conversation built as a cascade: speech-to-text, then a model, then text-to-speech, with interruption handling and a web search tool.

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

- **Replying.** When a turn ends, its transcript goes to the model. The model's text is pushed to text-to-speech as it streams, and the audio is forwarded to the client as it is produced.
- **Interrupting.** When the user starts speaking, the server cancels the model call and the speech stream, then sends an `interrupt` event. The client must stop playback and drop audio it has not played yet: speech is generated several times faster than it plays, so the client always holds some.
- **Searching.** With `PARALLEL_API_KEY` set, the model gets a `web_search` tool. Without it the tool is not offered and everything else works.

### Which model answers

A conversation is held with one model from start to end, chosen when it opens:

| Model | Provider | Needs |
| --- | --- | --- |
| `6 Luna` (the default) | OpenAI | `OPENAI_API_KEY` |
| `Haiku 5.5` | Anthropic | `ANTHROPIC_API_KEY` |
| `Sonnet 5.5` | Anthropic | `ANTHROPIC_API_KEY` |

`VOICE_MODEL` sets the model for every conversation, and a client can ask for another with `?model=` on the WebSocket URL. The test page passes its own query string on, so <http://127.0.0.1:8000/v1/voice/test?model=Haiku%205.5> talks to Haiku.

Everything below is the same for either provider. The conversation keeps one history, and only the last step differs: OpenAI is sent it as it stands, and for Anthropic it is translated into alternating user and assistant messages on every request. Four things do differ on an Anthropic model:

- **Thinking is fixed for the conversation.** It is off on Haiku 5.5, and on Sonnet 5.5 it is set to the nearest that model allows. The switch to a little thinking while a search is pending (rule 2 below) is not made: on this provider every switch makes the whole conversation be read again, and the gain was measured on the OpenAI model only.
- **Notes from the server travel in a user message**, wrapped in `<note>` tags that the instructions explain. The Messages API has no place for them where they fall, which is after a reply of the model's own.
- **Caching is asked for on every request.** Conversations shorter than the provider's minimum are not cached at all.
- **Forbidding a search in the last round costs a full read** of the conversation for that one request.

Opus 5.5 and Fable 5.2 are not offered: their thinking cannot be turned off, which is a wait before every reply.

The Anthropic side has been run against the real SDK with a fake server, and every scenario in the test suite has been replayed through it. It has not yet been run against the live API, so how it sounds and how fast it is are not known.

### What the model remembers

The history is what the model is sent on every call. It lives in memory for the life of the connection, nothing is stored at the provider, and it follows three rules.

1. **It holds what the user heard, not what was generated.** A reply joins the history only once it has been heard in full or cut off. A reply that is cut off is stored as the words that were heard, ending in "…", and the rest is never sent back. How much was heard comes from Cartesia's word timings and a server-side copy of the client's playback schedule, shifted by the sound-device delay the client reports (about 40 ms on laptop speakers, a few tenths of a second on Bluetooth). In live runs the stored text was within two words of the audio really played, and never past it. When a cut leaves twelve or more words unheard, a short note follows it saying what kind of thing the unheard part was ("the rest of the story", "the answer and its source"), never what it said.
2. **Every tool call keeps a valid result.** An interruption does not stop a search. Its call gets a placeholder result and the search carries on. What the user said is answered first. Then, at the next quiet moment, the real result is added as a call written by the server (a stub that names the original call) followed by the result, and the model tells the user about it in a reply of its own, without being asked. That holds even when the result came back while the user was still speaking: put in with their words, the model answered them and never mentioned it. A quiet moment is when the reply in progress has finished playing, so a result that lands during a long reply waits for it. If the reply about the result is itself cut off before a word of it is heard, it is made again. The model can stop a running search with `cancel_tool_call`. While a search has no result in the history yet, the model is asked to think a little before it speaks; otherwise it does not think at all. The first word of those replies came after 1.4 s at the median and 2.6 s for the slowest tenth, against 0.9 s and 1.1 s without thinking. Asked without thinking, it started the same search a second time in a third to a half of measured runs where the user only said "hello?"; with it, about one run in twenty. The switch also costs one prompt-cache miss, on the first request made with thinking on; the request after the switch back was served from the cache again.
3. **Nothing the model has been sent is changed afterwards**, with two exceptions. Each request starts with the whole of the one before it, and the instructions and tool list are identical every time, so a prompt cache stays valid. One consequence: what the model said before a search is kept in full even if the user cut it off part-way, because the model was already sent it. The exceptions:
   - The user's open turn, always the last item, can gain a line.
   - Once more than six search results are in the history in full, all but the newest two are replaced by a short marker when the user's turn ends. This breaks the cache on purpose, a whole batch at a time, to keep the context small.

The user's turn is put together before it reaches the model. A turn stays open until some of the reply to it has been heard, and words that arrive before then join it on a new line, because speech recognition sometimes splits one sentence in two. In live runs that was the first two to three seconds after the first half ended.

A one or two word fragment in a writing system the user has not spoken in during the conversation, starting within three seconds of the end of their sentence, is dropped as noise. So is one the recognizer has run on to the end of the sentence itself. This only works when the recognizer writes the noise in another script: it sometimes writes the same sound in Latin letters, and then nothing marks it as noise. A turn with no usable words puts back whatever it interrupted: an unanswered question is answered again, and a reply that was being heard is resumed. The limits on the filter matter, because a real word dropped as noise means the user is talked over.

The conversation is in [`app/entities/voice_ent.py`](app/entities/voice_ent.py). Speaking, and working out what was heard, is in [`app/entities/voice_speech.py`](app/entities/voice_speech.py).

## Try it

```bash
docker compose up -d                      # MongoDB and Redis
uv sync --extra dev
cp .env.example .env                      # then set CARTESIA_API_KEY and the model's key (OPENAI_API_KEY by default)
uv run uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000/v1/voice/test>, press **Start talking**, and allow the mic. This is a one-file test page, not a product UI. Use headphones if Cortex cuts itself off, which means the mic is picking up the speakers.

## The WebSocket contract

`WS /v1/voice/converse`

| Direction | Frame | Content |
| --- | --- | --- |
| client → server | binary | mic audio: raw 16-bit mono PCM at 16 kHz, about 100 ms per frame |
| server → client | binary | speech audio: raw 16-bit mono PCM at 24 kHz |
| client → server | text | optional `{"type": "playback", "delay": seconds}`: how long the client's sound device delays audio (in a browser, `AudioContext.outputLatency + baseLatency`) |
| server → client | text | JSON `{"type": ..., "text": ...}` |

| Event type | Meaning |
| --- | --- |
| `user.partial` | the user's words so far, while they are still speaking |
| `user.final` | the user's finished sentence; a reply follows |
| `assistant.delta` | the next piece of the reply's text |
| `assistant.done` | all of the reply's audio has been sent; `text` holds the full reply |
| `tool.call` | the assistant is searching the web; `text` holds the query |
| `interrupt` | the user started speaking: stop playback and drop queued audio. If that cut a reply short, `text` holds what the history keeps of it (just "…" if nothing); otherwise it is empty |
| `error` | something failed; `text` holds the message |

## Layout

Imports only go one way: `config ← core ← adapters ← entities ← routers ← main`.

```text
app/
  adapters/    one file per outside service; the only place an SDK is called
               openai_adp · anthropic_adp · cartesia_stt_adp · cartesia_tts_adp · parallel_adp · docdb_adp · redis_adp
  entities/    the logic: voice_ent, voice_speech and voice_model (the cascade), stt_ent, conversation_ent, user_ent
  routers/     HTTP and WebSocket endpoints: voice, cortex, user
```

Also still present from earlier experiments: user registration and login (`/v1/users`), projects and sessions (`/v1/cortex`), a transcription-only WebSocket (`/v1/cortex/transcribe`), and a Claude Agent SDK pipeline that no route uses.

## Development

```bash
uv run pytest
uv run ruff check app tests
uv run pyright
```

To check the model and the pipeline together without any audio, with the real OpenAI model and the real web search:

```bash
uv run python -m scripts.voice_pipeline_check
```

It fakes speech so each scenario can say how much of a reply was heard, and prints PASS or CHECK for 23 behaviours: context, search, cut replies, late search results, cancelling, noise, merged turns, clearing, another language, service failure and spoken style. A scenario that does not finish is shown as CHECK and the rest still run.

Configuration is read from the environment by `app/config.py`. `.env.example` lists every setting. Never commit credentials.
