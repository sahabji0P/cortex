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

- **Replying.** When a turn ends, its transcript goes to the model. The model's text is pushed to text-to-speech as it streams, and the audio is forwarded to the client as it is produced. Nothing is held back on the way: over ten real turns the first audio came 0.11 s after the model's first word, and the model's first word (1.1 s) was nearly all of the wait.
- **The speech connection is opened early.** One is opened when the call starts and again each time the user starts speaking, so it is there before the reply. Opening takes about 0.12 s and used to run while the model was thinking, so this only matters when opening is slow.
- **Who speaks first.** The user, unless the client asks otherwise: with `?first=assistant` on the WebSocket URL the assistant greets the user as soon as the call connects.
- **Interrupting.** When the user starts speaking, the server cancels the model call and the speech stream, then sends an `interrupt` event. The client must stop playback and drop audio it has not played yet: speech is generated several times faster than it plays, so the client always holds some.
- **Searching.** With `PARALLEL_API_KEY` set, the model gets a `web_search` tool. Without it the tool is not offered and everything else works.

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

`VOICE_MODEL` sets the model for every conversation, and a client can ask for another with `?model=` on the WebSocket URL. `GET /v1/voice/models` lists them, with whether each one's key is set. The test page has a provider and a model picker built from that list; a model whose key is missing is shown but cannot be chosen. A model can also be named in the page's address, as in <http://127.0.0.1:8000/v1/voice/test?model=Sonnet%205.5>. Asking for a model whose provider has no key closes the socket with code 1008 and the reason in words.

Everything below is the same for either provider. The conversation keeps one history, and only the last step differs: OpenAI is sent it as it stands, and for Anthropic it is translated into alternating user and assistant messages on every request. These things differ on an Anthropic model:

- **Thinking is fixed for the conversation.** It is off on Haiku 5.5, on Sonnet 5.5 it is set to the nearest that model allows, and Opus 5.5 is asked for as little as it will do. What a model writes as thinking is never sent back to it, because the history is cut down to what the user heard. The switch to a little thinking while a search is pending (rule 2 below) is not made, and is not needed: asked "hello?" over a running search, Haiku 5.5 and Opus 5.5 each started no second search in 20 runs of 20.
- **Notes from the server travel in a user message**, wrapped in `<note>` tags that the instructions explain. The Messages API has no place for them where they fall, which is after a reply of the model's own.
- **Caching is asked for on every request.** Conversations shorter than the provider's minimum are not cached at all.
- **Forbidding a search in the last round costs a full read** of the conversation for that one request.

Fable 5.2 is not offered: its thinking cannot be turned off, which is a wait before every reply. Opus 5.5 has the same limit and is offered anyway, for comparing.

Two smaller differences: the short note on an unheard remainder is always worded by Haiku 5.5, and two searches made in one round are sent back to an Anthropic model as two rounds, one after the other.

The Anthropic models are reached through the Claude API directly, with a key from platform.claude.com, never through a cloud provider. They have been run live: every request shape the conversation makes is accepted by Haiku 5.5, Opus 5.5 and Sonnet 5.5, and the real-model check below passes on Haiku 5.5 and Opus 5.5.

### What the model remembers

The history is what the model is sent on every call. It lives in memory for the life of the connection, nothing is stored at the provider, and it follows three rules.

1. **It holds what the user heard, not what was generated.** A reply joins the history only once it has been heard in full or cut off. A reply that is cut off is stored as the words that were heard, ending in "…", and the rest is never sent back. How much was heard comes from Cartesia's word timings and a server-side copy of the client's playback schedule, shifted by the sound-device delay the client reports (about 40 ms on laptop speakers, a few tenths of a second on Bluetooth). In live runs the stored text was within two words of the audio really played, and never past it. When a cut leaves twelve or more words unheard, a short note follows it saying what kind of thing the unheard part was ("the rest of the story", "the answer and its source"), never what it said.
2. **Every tool call keeps a valid result.** An interruption does not stop a search. Its call gets a placeholder result and the search carries on. What the user said is answered first. Then, at the next quiet moment, the real result is added as a call written by the server (a stub that names the original call) followed by the result, and the model tells the user about it in a reply of its own, without being asked. That holds even when the result came back while the user was still speaking: put in with their words, the model answered them and never mentioned it. A quiet moment is when the reply in progress has finished playing, so a result that lands during a long reply waits for it. If a noise cuts the reply about the result off before a word of it is heard, the same request is made again. If the user speaks over it instead, they are answered, and telling them of the result is left to the model: its instructions ask it to answer first and then tell them. The model can stop a running search with `cancel_tool_call`. While a search has no result in the history yet, the model is asked to think a little before it speaks; otherwise it does not think at all. The first word of those replies came after 1.4 s at the median and 2.6 s for the slowest tenth, against 0.9 s and 1.1 s without thinking. Asked without thinking, it started the same search a second time in a third to a half of measured runs where the user only said "hello?"; with it, about one run in twenty. The switch also costs one prompt-cache miss, on the first request made with thinking on; the request after the switch back was served from the cache again.
3. **Nothing the model has been sent is changed afterwards**, with two exceptions. Each request starts with the whole of the one before it, and the instructions and tool list are identical every time, so a prompt cache stays valid. One consequence: what the model said before a search is kept in full even if the user cut it off part-way, because the model was already sent it. The exceptions:
   - The user's open turn, always the last item, can gain a line.
   - Once more than six search results are in the history in full, all but the newest two are replaced by a short marker when the user's turn ends. This breaks the cache on purpose, a whole batch at a time, to keep the context small.

The user's turn is put together before it reaches the model. A turn stays open until some of the reply to it has been heard, and words that arrive before then join it on a new line, because speech recognition sometimes splits one sentence in two. In live runs that was the first two to three seconds after the first half ended.

A one or two word fragment in a writing system the user has not spoken in during the conversation, starting within three seconds of the end of their sentence, is dropped as noise. So is one the recognizer has run on to the end of the sentence itself. This only works when the recognizer writes the noise in another script: it sometimes writes the same sound in Latin letters, and then nothing marks it as noise. A turn with no usable words puts back whatever it interrupted: an unanswered question is answered again, and a reply that was being heard is resumed. For the second, the history gets one line saying that the sound had no words in it, and the model's instructions tell it to carry on from where it stopped. The limits on the filter matter, because a real word dropped as noise means the user is talked over.

The conversation is in [`app/entities/voice_ent.py`](app/entities/voice_ent.py). Speaking, and working out what was heard, is in [`app/entities/voice_speech.py`](app/entities/voice_speech.py). What differs between the providers is in [`app/entities/model.py`](app/entities/model.py), and the web search tool is in [`app/entities/tools.py`](app/entities/tools.py). Neither is particular to a call.

## Try it

```bash
docker compose up -d                      # MongoDB and Redis
uv sync --extra dev
cp .env.example .env                      # then set CARTESIA_API_KEY and the model's key (OPENAI_API_KEY by default)
uv run uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000/v1/voice/test>, press **Start talking**, and allow the mic. Pick a provider and a model first if you want one other than the default, and tick **Cortex speaks first** to be greeted. This is a one-file test page, not a product UI. Use headphones if Cortex cuts itself off, which means the mic is picking up the speakers.

## The WebSocket contract

`WS /v1/voice/converse`

| Query parameter | Meaning |
| --- | --- |
| `model` | the model to talk to: a `name` from `GET /v1/voice/models`. Left out, the configured one |
| `first` | who says the first words: `user` (the default) or `assistant`, which makes the assistant greet the user when the socket opens |

A value that is not offered, or a model whose provider has no key, closes the socket with code 1008 and the reason.

`GET /v1/voice/models` returns one object per model: `name` (what to send), `provider`, `label` (what to show), `available` (its key is set), `default` (used when none is named) and `listed` (whether a picker should offer it).

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
  entities/    the logic: voice_ent and voice_speech (the cascade), model (either provider's model, for any conversation), tools (the tools a model may call, each a small object), user_ent
  routers/     HTTP and WebSocket endpoints: voice, user
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

It fakes speech so each scenario can say how much of a reply was heard, and prints PASS or CHECK for 24 behaviours: context, search, cut replies, late search results, cancelling, noise, merged turns, clearing, another language, service failure and spoken style. A scenario that does not finish is shown as CHECK and the rest still run.

Configuration is read from the environment by `app/config.py`. `.env.example` lists every setting. Never commit credentials.
