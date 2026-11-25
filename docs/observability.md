# Observability: a gem's log

Every gem has a log: everything that happens with it, written down in one list, in the
order it happens. Every message and every turn, every request to the model with what it
was sent, its tokens, its cost and how long it took, every tool call from start to end,
and every change of model, instructions or tools. Nothing is sampled, and nothing is
summed up and lost.

The app draws it as the gem's **trajectory**, a channel beside its chat, and adds it up
for Home's figures. This page says what the log holds, what it promises, and how to read
it. Every kind of entry and its fields are in
[the contract](chat-contract.md#a-gems-log).

## What it answers

- **What exactly was this request sent?** Every item, the instructions layer by layer,
  and every tool as the model was told of it: `GET /v1/gems/{id}/log/{seq}/sent`.
- **What did it cost?** The tokens each request was charged for (read in full, read from
  a cache, written to a cache, output, and thinking), and their cost in US dollars.
- **Where did the time go?** When each request was sent, when its first token came, when
  it ended, and how long each tool ran.
- **What did the model say, and what did the user get?** A request's end holds all the
  model produced. The turn's `gem.said` holds what was delivered: what the user heard in
  a call, or was sent in writing. They differ when a reply was cut.
- **How did a reply end?** `finished`, `cut` or `failed`, with the error.
- **What became of a tool call?** Started, then done; or sent to the background when its
  reply was cut, then done, then delivered to the gem, cancelled, or lost to a restart.
- **What changed, and when?** A new model, an edited instruction, a connection whose
  tools changed, a switch from the API key to the ChatGPT proxy: each is written down
  before the first request it applies to.

## An example

A written message asks about the weather. The gem starts a search and is cut off by a
second message, which it answers while the search runs. When the search comes back, the
gem tells the user what it found, unasked. This is the log that run leaves, as the code
writes it (from the test suite's stand-in model and search):

```text
seq turn kind               of note
  0  -   settings.changed   model 6 Luna via api; 3 layers of instructions; 2 tools
  1  -   note.written       "It is Sunday, 22 November 2026, 4:47 pm UTC. ..."   (time-line)
  2  1   turn.opened        by user
  3  1   user.said          "weather?"
  4  1   request.sent       request 1, sent 2 items
  5  1   request.done       "Let me look." and web_search {"query": "Pune weather"}
  6  1   tool.started       call_1, web_search
  7  1   tool.backgrounded  call_1: the reply was cut, the search carries on
  8  1   gem.said           "Let me look."
  9  1   tool.called        call_1
 10  1   tool.result        call_1: the placeholder, "Still running in the background ..."
 11  1   turn.closed        cut
 12  2   turn.opened        by user
 13  2   user.said          "and the time?"
 14  2   request.sent       request 2, sent 6 items, think true: a search is still running
 15  2   request.done       "Noon."
 16  2   gem.said           "Noon."
 17  2   turn.closed        finished
 18  1   tool.done          call_1, done in the background, with its result
 19  -   tool.called        a stub that delivers call_1's result
 20  -   tool.result        the result
 21  3   turn.opened        by background_result
 22  3   request.sent       request 3, sent 9 items
 23  3   request.done       "It is sunny in Pune."
 24  3   gem.said           "It is sunny in Pune."
 25  3   turn.closed        finished
```

The search can be read from start to end by its `call_id`, though its entries are spread
over three turns, and each still names the turn that made it. A request on a real
model ends like this. The tokens here are made up; the cost is what
[`prices.py`](../app/entities/prices.py) works out for them on 6 Luna:

```json
{
  "seq": 23, "at": "2026-11-22T16:47:26.912Z", "kind": "request.done", "turn": 3,
  "request": 3, "attempt": 1, "outcome": "done",
  "text": "It is sunny in Pune.", "calls": [],
  "first_token_at": "2026-11-22T16:47:25.804Z",
  "usage": {"input": 412, "cache_read": 3072, "cache_write": 0, "output": 58, "thinking": 0},
  "cost_usd": 0.00010092, "priced": "2026-11-20", "error": null
}
```

## Two kinds of entry

Every entry has one shape: its number (`seq`), when it was written (`at`), its `kind`,
the `turn` it belongs to, and its own fields. Each thing is written once, as the kind of
entry it is, at the moment it is final.

| | Kinds | Sent to a model? |
| --- | --- | --- |
| **What the gem remembers** | `user.said`, `note.written`, `gem.said`, `tool.called`, `tool.result` | Yes. The gem's conversation is these entries since the latest reset, in order, and nothing else |
| **What happened** | `call.opened`, `call.closed`, `gem.reset`, `turn.opened`, `turn.closed`, `settings.changed`, `request.sent`, `request.done`, `tool.started`, `tool.backgrounded`, `tool.done` | Never |

So the log is not a copy kept beside the conversation: it *is* the conversation, with
everything else that happened written in between. A `gem.reset` is where the user reset
the gem: the conversation is what the gem remembers after the latest one, and
what came before stays in the log.

## What it promises

- **Order is the truth.** Entries are numbered in one place for each gem, in the order
  things happened. Everything the gem remembers is added before the request that carries
  it, so the log's order is the order the model was sent things in. Order by `seq`,
  never by `at`.
- **A request holds a count, not a copy.** It was sent every remembered entry before it,
  since the latest reset, and `count` says how many. That is how `/sent` rebuilds it,
  and how a lost entry shows: the numbers do not add up, and `exact` is false.
- **A change comes before what it applies to.** What a reply or a call is made with (the
  model and the way to its provider, each layer of the instructions, every tool) is
  compared with what was last written down, and only the part that differs is written,
  in full. The way to the provider is compared again at each request, since it can
  change in the middle of a call. Comparing what is used, not when a setting is edited,
  also catches what nobody edited, such as a connection whose tools were read again.
- **Unknown is never zero.** A request cut while the model was still producing has no
  token count, and a model with no price has no cost. Both are null.
- **Writing it costs a reply nothing.** Each entry is told at once to any socket that
  watches the gem, and written to the database behind the reply's back. A write of what
  happened that fails is logged and dropped: it fails no reply. What the gem remembers is
  held and written again, in order, and a reply's last frame and a call's end wait until
  it is stored. If it cannot be, the reply or the call fails.
- **It changes nothing.** With a log or without one, the model is sent the same: a test
  sends a scripted reply both ways and compares the requests.

## Reading it

| Route or frame | Gives |
| --- | --- |
| `GET /v1/gems/{id}/log` | A page of entries, oldest first: the latest 200, or `limit` (up to 500) before a `seq`. With the settings in force before the page's first entry |
| `GET /v1/gems/{id}/log/{seq}` | One entry, in full |
| `GET /v1/gems/{id}/log/{seq}/sent` | Everything that request was sent |
| `log.watch` on `/v1/link` | Each entry of a gem (or of every gem) as it is written, in `log.entry` frames. `log.unwatch` stops it |

```bash
curl -s localhost:8000/v1/gems/$GEM/log | jq '.entries[] | {seq, kind, turn}'
curl -s localhost:8000/v1/gems/$GEM/log/22/sent | jq '.items | length'
```

## What it does not show yet

- **Retries a provider's library makes by itself.** A request that fails before its
  reply starts is tried up to twice more, unseen; the log shows one request, and its
  wait for the first token includes the tries before.
- **Speech.** Speech-to-text and text-to-speech have their own times and their own cost,
  and are not written down yet. Nor is the cost of a web search.
- **The tokens of a reply cut while the model was still producing.**
- **Thinking a provider does not hand over**, and whatever the ChatGPT proxy does on its
  own side.
- **Totals.** The backend keeps none yet: the app adds up what it has read.

These are in the [notes](darbar-notes.md) as what comes next.

## Adding to it

The log is the base for anything that looks at what a gem did. A new thing a gem does (a
new step of the loop, a new paid service) is a new kind of entry, written through
[`gem_log.py`](../app/entities/gem_log.py), never a store of its own. The loop offers
hooks to whoever records a reply ([`loop.py`](../app/entities/loop.py)). A new kind, or
a new field of one, is a change to the contract: add it to
[the contract](chat-contract.md#a-gems-log), and the app follows.

The entries are kept in the `GemLog` collection by
[`conversation_store.py`](../app/entities/conversation_store.py). The tests that pin it
down are `tests/test_gem_log.py` and `tests/test_log_traces.py`.
