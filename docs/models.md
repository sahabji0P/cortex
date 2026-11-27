# Models and providers

Which models a gem can answer with, how the two providers differ, how OpenAI can be
reached through a ChatGPT login, and how a request's cost is worked out.

## The models

A conversation is always held with its gem's model. A gem that names none gets
`VOICE_MODEL`. The model does not change in the middle of a call.

| Model | Provider | Thinks before it speaks | Needs |
| --- | --- | --- | --- |
| `6 Luna` (the default) | OpenAI | no | `OPENAI_API_KEY`, or the proxy |
| `6.1 Terra` | OpenAI | no | `OPENAI_API_KEY`, or the proxy |
| `6.1 Sol` | OpenAI | a little: it cannot be turned off | `OPENAI_API_KEY`, or the proxy |
| `Haiku 5.5` | Anthropic | no | `ANTHROPIC_API_KEY` |
| `Sonnet 5.5` | Anthropic | as little as it allows | `ANTHROPIC_API_KEY` |
| `Opus 5.5` | Anthropic | a little: it cannot be turned off | `ANTHROPIC_API_KEY` |

`GET /v1/voice/models` lists them, one object each: `name` (what to send), `provider`,
`label` (what to show), `available` (its key is set), `default` (used when none is
named) and `listed` (whether a picker should offer it). A call with a gem
whose model has no key gets `call.refused`, and a message gets `chat.error`, with the
reason in words.

Fable 5.2 is not offered: its thinking cannot be turned off, which is a wait before
every reply. Opus 5.5 has the same limit and is offered anyway, for comparing.

The Anthropic models are reached through the Claude API directly, with a key from
platform.claude.com, never through a cloud provider.

The models are in [`app/entities/model.py`](../app/entities/model.py), and each
provider's SDK is called only from its adapter
([`openai_adp.py`](../app/adapters/openai_adp.py),
[`anthropic_adp.py`](../app/adapters/anthropic_adp.py)).

## One history, two providers

A conversation keeps one history, and only the last step differs. OpenAI's Responses API
is sent it as it stands. For Anthropic's Messages API it is translated on every request
into alternating user and assistant messages
([the spec](voice-spec.md#the-same-history-for-either-provider) has the table). Nothing
is stored at the provider.

These things differ on an Anthropic model:

- **Thinking is fixed for the conversation.** It is off on Haiku 5.5, as near to off as
  Sonnet 5.5 allows, and as little as Opus 5.5 will do. What a model writes as thinking
  is never sent back to it, because the history holds only what was delivered.
- **No thinking while a search runs.** On OpenAI the model is asked to think a little
  while a search has no result yet, so it does not start the same search again. On
  Anthropic this is not done, and live runs showed it is not needed
  ([measured](voice-spec.md#measured-in-live-runs)).
- **Our lines travel in a user message**, wrapped in `<note>` tags that the instructions
  explain. The Messages API has no place for them where they fall, which is after a
  reply of the model's own.
- **Caching is asked for on every request** (see [Caching](#caching)).
- **Two searches made in one round** are sent back as two rounds, one after the other.

## Caching

Each request starts with the whole of the one before it, so it can read what that one
cached. A cache read costs a tenth of the input price or less, and writing one a quarter
more than input (see [Cost](#cost)).

- **OpenAI caches by itself.** It writes up to the end of the request's last message
  that it takes as a stopping place: a user message or a tool's result, not a note of
  ours after the start. Nothing is asked for. A request shorter than 1,024 tokens is not cached.
- **OpenAI's explicit marks are not used.** With them, a request reads only up to a mark
  it carries itself, so one mark at the end of each request would never be read. Through
  the proxy they have no effect: even a lifetime OpenAI does not offer is taken.
- **Through the proxy** reads come in steps of 128 tokens and not on every request, and
  no write is reported.
- **Anthropic caches only what is marked.** Every request is marked at its end. The
  provider looks for what was cached in at most 20 blocks from a mark, and each call and
  each result of a round is a message of its own, so a request more than 18 blocks long
  has a second mark 18 blocks before its end. A round of up to some 18 calls still reads
  the cache ([measured](voice-spec.md#measured-in-live-runs)). A request shorter than 512
  tokens is not cached.
- **How long a cache lasts** is taken to be 5 minutes from its last use on Anthropic
  (ours ask for no longer) and 30 minutes on OpenAI (its default from GPT-5.6 on; through
  the proxy it is not known). After that, when the model, the instructions or the tools
  change, or after a compaction, a conversation starts cold (see
  [the contract](chat-contract.md#a-cold-start)).
- **A compaction made while the cache is warm reads it.** The summary request is sent
  the same instructions, tools and items as the gem's next reply would be, with one line
  of ours after them and tool use off (`tool_choice` none). Turning tool use off was
  measured not to lose the cache on Anthropic: on Haiku 5.5, Sonnet 5.5 and Opus 5.5 such
  a request read everything the request before it wrote. On OpenAI it was not measured.
  The request after a compaction starts with the summary, so it writes the cache again:
  it is a cold start, with the reason `compaction`.
- **A summary has more room than a reply.** On Anthropic a reply may write at most 8,192
  tokens, thinking included, and one that reaches it fails. The summary request may write
  32,000, which every model a gem can use allows. A larger ceiling was measured not to
  lose the cache: on Haiku 5.5 the summary request read everything the reply before it
  wrote. OpenAI is sent no ceiling.

## OpenAI through a ChatGPT login

OpenAI can also be reached through a proxy that speaks its API, such as
[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) signed in with a ChatGPT
account, so the ChatGPT plan pays instead of the API key.

- **Set it up** with `OPENAI_PROXY_URL` (for CLIProxyAPI on this Mac,
  `http://127.0.0.1:8317/v1`) and `OPENAI_PROXY_KEY` (one of the proxy's own keys).
- **Choose between them.** With the proxy and `OPENAI_API_KEY` both set, the user picks
  one in the app. `GET /v1/providers/openai` says which is in use and which are set up,
  and `PUT` with `{"via": "api"}` or `{"via": "proxy"}` switches it from the next
  request on, for calls in progress too. The choice is kept across restarts. Until one
  is made, the API key is used. A way that is not set up gets 409.
- **The plan's limits apply.** Its models must include the gem's, and its usage limits
  apply to calls. A reply through the proxy was measured to start much later than one
  with the API key, which is why the key is the default (see the
  [notes](darbar-notes.md)).

### Changing the proxy's account

With `OPENAI_PROXY_MANAGEMENT_KEY` set to CLIProxyAPI's management key
(`remote-management.secret-key` in its config), the app can change the proxy's ChatGPT
account:

1. `GET /v1/providers/openai` lists the proxy's accounts, each with its plan and whether
   the proxy can still use it.
2. `POST /v1/providers/openai/sign-in` starts a sign-in and gives the address to open in
   the browser. The browser comes back to the proxy, not to this backend.
3. `GET /v1/providers/openai/sign-in/{state}` says `wait`, `ok` or `error`. When it is
   `ok`, the account just signed in replaces the proxy's other ChatGPT logins, so every
   request uses it.

No login ever reaches this backend's database or the app. The way in is in
[`app/entities/providers_ent.py`](../app/entities/providers_ent.py), and the proxy's
management API in [`app/adapters/cliproxy_adp.py`](../app/adapters/cliproxy_adp.py).

## Cost

No provider reports a cost. Both report the tokens they charged for with every reply:
read in full, read from a cache, written to a cache, and output. The backend works the
cost out from its own price table, [`app/entities/prices.py`](../app/entities/prices.py),
which is kept by hand and dated.

- **Unknown is never nothing.** A model with no price has no cost, and a request cut
  while the model was still producing has no token count (it is read from a reply that
  finished). Both are kept as unknown.
- **Long requests.** Where a provider charges a long request more (OpenAI above 272K
  tokens sent, Haiku 5.5 above 100,000), the whole request is costed at the higher
  prices.
- **Through the proxy** the plan pays. The cost shown is what the request would have
  cost with the API key, except that the proxy reports no cache writes: what was written
  is priced as plain input.
- **To change a price**, change it in the table and move `PRICED` on.

A request's tokens and cost are written to the gem's log
([Observability](observability.md)). Speech and search are not costed yet.

From the same table, a gem's context estimates what is to come
([`/context`](chat-contract.md#get-v1gemsgem_idcontext)): what its next request would
cost with the conversation kept and the cache gone (writing all of it to the cache), and
what compacting it would cost (the summary request reading a warm cache or writing a
cold one, about 2,000 tokens written out, then about 2,000 written to the cache by the
next request). Both are unknown where a price is.
