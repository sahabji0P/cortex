# Evals and observability: a proposal

Written on 17 November 2026, against `feat/darbar-layer` at `c13fe14`. Nothing here is
built. It is for the owner to read and decide on.

The recommendation, the steps and the decisions are what to read. The three parts
between them are the reasoning, to look things up in.

Two words, as used here:

- **Observability**: a record of what happened in real use. How long a reply took, what
  it cost, what was called.
- **An eval**: a scripted conversation run on purpose, several times, to answer one
  question about how a gem behaves.

How sure each statement is:

| Mark | Meaning |
| --- | --- |
| [code] | I read it in this repo or in the app's repo |
| [docs] | Read on the provider's or the tool's own page on 17 November 2026. A research pass did the reading and gave the link. I opened the OpenAI and Cartesia price pages again myself, and not the rest. The number after it points to [Sources](#sources) |
| [assumed] | My own reasoning or arithmetic, or second-hand. Not checked |

## The recommendation

1. **Build it here, small. Adopt no platform yet.** A few plain files and two small
   scripts answer every question on the list. The tools that would fit are either hosted,
   so your words leave the Mac, or heavier than Cortex itself. The standard they share is
   still changing. The record below uses that standard's names, so sending it to a tool
   later is a small job, not a rewrite.
2. **One record, used twice.** Every model call, tool call and reply writes one line.
   A live call writes these lines. An eval writes the same lines, plus which case it was.
   So an eval is a scripted conversation and some checks over that record. Whatever an
   eval measures, real use can be watched for too.
3. **Record at the two doors the loop already goes through, not in the loop.** The loop
   is handed a model and a toolbox. Hand it wrapped ones that keep time and count tokens.
   The loop's own code does not change.
4. **Evals live in `evals/`, are run by hand, and say what they will cost first.** They
   never run under `pytest`. The unit tests keep their scripted model.
5. **Numbers always, words only when asked for. Nothing leaves the Mac.** This repo is
   public (checked on GitHub), so no record and no transcript is ever committed.
6. **First step, one day: the record.** **Second step: the eval runner**, and the first
   live run: the three behaviours never run live, on the two cheapest models, for about
   20 cents [assumed].

### What I found on the way that changes the picture

- **The model layer throws the token counts away.** `RoundDone` carries only the tool
  calls [code]. The existing check has to tap the adapters to see them [code]. This is
  the first thing to fix.
- **A reply that is cut off reports no full token counts.** Barge-in closes the stream
  before the provider sends them. Anthropic gives the input half at the start, OpenAI
  gives nothing. Cut replies are normal in a call. So the cost of a call can only ever
  be an estimate. See [Money](#money).
- **On the cheap models, speech costs far more than the model.** About thirty times
  more per reply on 6 Luna or Haiku 5.5 [assumed, from list prices]. A cost page that
  shows only tokens would miss most of the bill.
- **Today's instructions are small.** About 750 tokens with the tools [assumed: counted
  as characters divided by four]. That is under OpenAI's smallest cacheable size of
  1,024 tokens [docs 9], so the first turns of an OpenAI call are not cached at all.
- **Of the six questions, two need code that does not exist yet**: thinking kept and
  sent back, and the three text questions.

---

## Part 1. Observability

### What is recorded

One line of JSON per thing that takes time or costs money, and one line per reply that
ties them together.

| Line | One per | What it holds |
| --- | --- | --- |
| `conversation` | call or written conversation | gem id and name, surface, model, tools offered, the size of each layer of the instructions and a hash of the whole, when it opened and closed, how many turns had no words, how many fragments were dropped as noise |
| `reply` | reply | what started it (the user, a greeting, a late result, a sound with no words), the wait for the first word, the wait for the first sound, rounds, tool calls, words said, words heard, how it ended (heard, cut, cut before a word, failed), cost |
| `round` | model call | the model asked for and the model id the provider answered with, whether it was asked to think, items sent, time to the first text, total time, tokens in, of those how many were read from the cache and how many written to it, tokens out, of those how many were thinking, words said before a tool call, how it ended (done, cut, failed), cost, whether the cost is an estimate |
| `tool` | tool call | tool name, how long it took, whether it failed, whether it outlived its reply, cost |
| `speech` | reply, in a call | characters sent to text-to-speech, seconds of audio made, cost |
| `listening` | user's turn, and once more when the call closes | seconds of audio sent to speech-to-text since the last such line, cost |

Every line also carries the time, the conversation's id, the reply's number and the
gem's id. The gem's id is the key for "per gem": a gem's name can change, its id does
not [code]. A call without a gem has no gem id and is counted as Cortex.

One `round` line, as an example:

```json
{"kind": "round", "at": "2026-11-17T09:14:03.211Z", "conversation": "c_8f2a", "reply": 4,
 "gem": "6739e1…", "model": "Haiku 5.5", "model_id": "claude-haiku-5-5", "think": false,
 "items": 9, "first_text_ms": 640, "total_ms": 1180,
 "input_tokens": 2310, "cached_tokens": 1890, "cache_write_tokens": 420,
 "output_tokens": 46, "thinking_tokens": 0, "words_before_call": 5,
 "ended": "done", "cost_usd": 0.00009, "estimated": false}
```

The heading for an unheard remainder is a model call too. It gets a `round` line marked
as such, so it is counted.

### The times, exactly

```text
user stops ──▶ turn.end ──▶ request sent ──▶ first word ──▶ first sound sent ──▶ first sound heard
(not seen)        t0                             t1               t2            t2 + 0.15 s + device delay

wait for the first word  = t1 − t0
wait for the first sound = t2 − t0
```

- `t0` is when the server learns the turn ended. `t1` is the model's first piece of
  text. `t2` is when the first chunk of audio leaves for the client, in
  `Speech._forward` [code].
- When the user hears it is `t2` plus the client's delay. The server already estimates
  that delay for working out what was heard [code]. The record keeps `t2` and the delay
  apart, so the measured part stays clean.
- **A blind spot.** The server cannot see the gap between the user going quiet and
  `turn.end`. That gap is the speech-to-text service deciding the turn is over. The user
  feels it as part of the wait. Only real audio can measure it: see
  [real audio](#driving-a-call-without-audio-and-with-it-later).
- **A real clock of its own.** Evals move the call's clock by hand to decide what was
  heard [code]. Times in the record must come from a real clock, never that one.

### Where the record is made

The loop is the right place to look from, because every kind of conversation passes
through it. But the loop should not learn about time, tokens or files. It does not have
to. It is handed its model and its toolbox on every reply [code]:

```python
record = self._records.reply(started_by="user", turn_ended_at=now)
await run_reply(reply, record.model(self._model), record.tools(self._tools), ...)
```

- `record.model(...)` wraps the model. It notes when a round starts, when its first
  text comes, what the provider counted, and whether the round finished or was cut.
- `record.tools(...)` wraps the toolbox. It notes each call's name, its time, and
  whether it failed.
- The loop sees a model and a toolbox, as before. Not one line of `loop.py` changes.

This is what the `Recorder` in `scripts/voice_pipeline_check.py` already does, by hand,
for the check [code]. The proposal makes it the real thing.

Three things have to be added around it:

1. **`RoundDone` carries the provider's counts.** Both adapters already pass the events
   that hold them [code]. The model layer just has to keep them.
2. **The conversation adds what only it knows**: when the turn ended, when the first
   sound left, how much was heard and how the reply ended. The call knows the last two
   in `_settle` [code].
3. **A tool call that outlives its reply** writes its line when it finishes, with the
   number of the reply that made it. That is why `tool` is a line of its own and not a
   field of the reply.

Why not have the loop send out events of its own? It would then need to know what a
clock is and what usage is. That is the second job. Wrapping keeps it to one.

### Money

**Prices live in one small file**, with the date they were read. The record keeps the
raw counts as well as the cost, so a cost can be worked out again when a price changes.

List prices, per million tokens [docs 9, 10, 11, 12]:

| Model | In | Read from cache | Out |
| --- | --- | --- | --- |
| 6 Luna | $0.10 | $0.01 | $0.50 |
| 6.1 Terra | $1.00 | $0.05 | $5.00 |
| 6.1 Sol | $2.00 | $0.10 | $10.00 |
| Haiku 5.5 (requests up to 100,000 tokens) | $0.10 | $0.01 | $0.50 |
| Sonnet 5.5 | $2.00 | $0.10 | $10.00 |
| Opus 5.5 | $4.00 | $0.20 | $20.00 |

- Writing to the cache costs 1.25 times the input price on both providers [docs 9, 11].
- **The two providers count input differently.** OpenAI's `input_tokens` includes the
  cached ones [docs 9]. Anthropic's leaves them out: the whole input is `input_tokens`
  plus `cache_read_input_tokens` plus `cache_creation_input_tokens` [docs 11]. The
  record stores one meaning for both: **all tokens sent, with the cached ones as a part
  of them.** That is also what the app's cost page expects [code].
- Cartesia bills in credits. Listening with ink-2 costs 3 credits for each second of
  audio, silence included. Speech costs about 1 credit per character [docs 13]. A
  million credits cost between about $37 and $50 inside a plan, and up to $65 beyond
  it [docs 14]. Which plan you are on is a setting.
- A search in turbo mode costs $1 per thousand [docs 15].

**What one reply costs** [assumed: my arithmetic, at $40 per million credits]. Take a
reply of 150 characters, 1,500 tokens in, 50 out, nothing cached:

| Part | Cost |
| --- | --- |
| Speech, 150 characters | $0.006 |
| Listening, per minute the call is open | $0.007 |
| The model, on 6 Luna or Haiku 5.5 | $0.0002 |
| The model, on Opus 5.5, before its thinking | $0.007 |
| One search | $0.001, plus its result as input tokens on later rounds |

So a ten-minute call on a cheap model costs about 20 cents, nearly all of it Cartesia.
Two things follow. The record must count speech, not only tokens. And evals that fake
speech cost almost nothing.

**What cannot be counted exactly:**

- **A round that is cut off.** The provider's counts are read at the end of the
  stream [code], and a cut closes it first. The record marks such a round
  `"ended": "cut"`, `"estimated": true`. It works the cost out from the size of what
  was sent and the text received. Anthropic sends the input counts at the start of the
  stream [docs 21], so for its models that half can be kept exactly. I assume a cut
  round is still billed. Neither provider's page says so. See
  [What I could not find out](#what-i-could-not-find-out).
- **Speech that is cut off.** Whether Cartesia bills characters that were sent but
  cancelled is not documented. The record counts characters sent.

So the cost shown is an estimate, and it should say so. The bill is the truth. Once a
month, compare the month's total with each provider's own dashboard. If they agree
within a tenth, the estimate is good enough.

### The `usage` event

The app already has this event's shape, with tests [code]. The backend should send
that shape and not invent another. One event per paid call:

```json
{"type": "usage", "text": "", "usage": {
  "service": "model", "provider": "Anthropic claude-haiku-5-5",
  "quantity": 2356, "unit": "tokens", "cost_usd": 0.00009,
  "input_tokens": 2310, "cached_tokens": 1890, "output_tokens": 46,
  "cache_write_tokens": 420, "estimated": false}}
```

| Field | Meaning |
| --- | --- |
| `service` | `stt`, `model`, `tts` or `search` |
| `provider` | the provider and the model, to show |
| `quantity`, `unit` | how much was used: tokens, seconds, characters or requests |
| `cost_usd` | worked out by the backend. The app knows no prices [code] |
| `input_tokens` | model only: all tokens sent, cached or not |
| `cached_tokens` | model only: the part of `input_tokens` read from the cache |
| `output_tokens` | model only: tokens written, thinking included |
| `cache_write_tokens` | **new**, model only: the part of `input_tokens` written to the cache, billed at 1.25 times |
| `estimated` | **new**: true when the provider gave no count, as for a cut round |

The two new fields are optional. The app's reader has no rule against fields it does
not know [code], so it would pass over them until it wants them.

**When each one is sent** matters, because the app puts a `usage` event on its latest
turn [code]:

| Service | Sent | Why then |
| --- | --- | --- |
| `search` | the moment the search returns | The app takes it as the sign the search is over [code]. Today it has to guess |
| `model` | at the end of each round | |
| `tts` | when the reply ends or is cut | |
| `stt` | just after `user.final` | The turn it belongs to exists by then |

A `usage` event that arrives before any turn exists is dropped by the app [code]. That
loses a few seconds of listening at the start of a call, which is fine.

Times are left out of this event. The app has no screen for them yet. They are in the
record, and can be sent when a screen wants them.

**This is a change to the contract** in `AGENTS.md`: one new event type and one new
field on `VoiceEvent`. It needs a matching pull request in the app. This document only
proposes it.

### Where it is kept, and how it is looked at

- **Kept as files**: `~/.cortex/records/2026-11-17.jsonl`, one file per day, one line
  per record. The folder is a setting.
- **Why outside the repo.** The repo is public. And work here happens in worktrees
  that get deleted.
- **Why not MongoDB**, which is already running. Files need no schema and no
  migration. They can be read with `grep`. And evals should run without Docker.
- **Size.** A line is about half a kilobyte. A thousand replies is a few megabytes
  [assumed].
- **A name to avoid.** The app already uses `CORTEX_DATA_DIR` for its own folder
  [code]. The backend's setting needs another name.

How it is looked at: one small script with three views.

| View | Shows |
| --- | --- |
| Per conversation | each reply on one line: the wait for the first word and the first sound, rounds, tools, tokens, the share read from the cache, how it ended, cost |
| Per gem | calls, replies, the middle and the slow tenth of the wait for the first word, the share of replies cut, cost by service |
| Over time | the same per day. Also the longest reply in rounds, which is the watch on looping |

Anything else is a query. DuckDB reads these files as a table with no setup [assumed].

### Privacy

These are your own conversations and your own spending. The rule is: **numbers always,
words only when you ask, and nothing leaves the Mac.**

| | |
| --- | --- |
| Always recorded | times, token counts, costs, model names, tool names, sizes, how a reply ended, ids |
| Not recorded | what you said, what the gem said, search queries, search results, a gem's instructions (their size and a hash only) |
| Recorded when a setting is on | the words, in a separate folder, deleted after a number of days you choose. For chasing one bad call |
| Never in the repo | any record, any transcript |
| Never sent anywhere | the record. No hosted tool, no tracing library that reports home |

- **Search queries are words too.** They say what you were curious about. They are
  left out by default.
- **The words of a call are already kept once**, by the app, in its sessions folder
  [code]. The backend does not need a second copy by default.
- **What already leaves the Mac does not change**: audio to Cartesia, text to the model's
  provider, queries to Parallel. OpenAI is already told to store nothing [code].
- **An error message can carry words.** The record keeps the kind of error and a short
  cut of its message.
- **If Cortex is hosted later**, the record sits on the host beside the conversation,
  under the same login. It is still not handed to a third party.

---

## Part 2. Evals

### The four kinds

| Kind | Asks | Example | Judged by |
| --- | --- | --- | --- |
| Behaviour | Does the gem do the thing, in a scripted conversation? | Does it say a few words before a search? | checks on the record and the history |
| Speed | How long is the wait, and what moves it? | First word against the size of what is sent | times in the record |
| Cost | What does a turn cost, and how much comes from the cache? | Tokens per turn over a long conversation | counts in the record |
| Comparison | Is A better than B? | Two wordings of one instruction. Two models | any of the three above, on both, in one run |

A comparison is not a fourth kind of measurement. It is any of the first three, run on
two variants side by side.

### What a case looks like on disk

Three kinds of thing, in three places:

```text
evals/
  cases/        one Python function per case: the script of a conversation, and its checks
  wordings/     text files: the versions of an instruction that are compared
  fixtures/     stored search results, so a search is free and the same every time
  results/      one short summary per run that settled something: numbers only
```

A case is a short Python function:

```python
@case(
    asks="Does the gem say a few words before a search, so the wait is not silent?",
    search=canned("harbour_cup.txt"),
)
async def words_before_a_wait(call: Call) -> Verdicts:
    await call.ask("Who won the Harbour Cup this year?")
    first = call.rounds[0]
    return {
        "searched once": call.count("web_search") == 1,
        "spoke before searching": 1 <= first.words_before_call <= 12,
        "named the winner": "Kestrel" in call.last_reply,
    }
```

**Why Python and not a data file.** A conversation's steps depend on what happens:
"wait until the search has started, then speak over it". That is a script. A data
format would need its own small language and something to run it. The check already
has fifteen scenarios written this way [code]. What must be data is what gets compared
and counted, and that is: wordings are text files, results are lines of JSON.

**Why stored search results.** A real search gives a different result each time and
costs money. A stored one is free and the same every run. Its facts are known, so "did
the gem tell the result" becomes an exact check and needs no judge. **The facts in it
are made up**, like the Harbour Cup and its winner above. A real fact could come from
the model's memory, and then the check would pass without the result ever being told.
A stored search can also be held back, released or made to fail, as the check's search
stand-in already can [code]. One smoke case keeps the real search.

**What a variant can change**: the model, the text of one layer of the instructions,
the gem's config, the tools offered. Later, a switch in the code, for the text
questions.

### Driving a call without audio, and with it later

**Now.** As the check does [code]:

- Speech-to-text is a stand-in. The case says "the user starts speaking" and "the user
  finished, and said this".
- Text-to-speech is a stand-in that takes one second per word.
- The clock is moved by hand. So the case decides how much of a reply was heard.
- The model is real. Search is stored, or real.

This covers everything about how the model behaves and what goes into the history. It
cannot see wrong transcripts, the wait for the turn to end, real speech timing, or the
gem hearing itself.

**Later, in three steps. Each costs more and shows more.**

| Step | What is real | What it shows | What it needs |
| --- | --- | --- | --- |
| 1 | text-to-speech | the true wait for the first sound | Cartesia credit. Nothing new to build |
| 2 | speech-to-text too, fed sound files | turn detection, noise, split sentences, wrong words | sound files of the user's side: recorded once, or made with text-to-speech. They are sent in real time, so a case takes as long as it lasts |
| 3 | the WebSocket | the contract with the app, end to end | a small client that does what the test page does |

Step 2 is how others do it too: make the user's speech, send it in, and write down
what comes back [docs 16]. No tool is needed for it.

### How a result is judged

In this order. Use the first that can answer.

1. **An exact check**, wherever one can. The record and the history already hold most
   answers: how many searches, words before a call, the order of items in the history,
   a fact from a stored result, no markup in spoken text, the length of a reply.
2. **A model as judge**, when the question is about meaning. "Does the reply carry on
   from where it was cut, or start again?"
   - One yes-or-no question per property, with one line of reason.
   - The judge is from the other provider than the model under test. A model tends to
     prefer text like its own [docs 20].
   - It judges saved transcripts after the run. So changing the judge's question costs
     only the judge, not a new run.
   - Before it is trusted, you label twenty transcripts yourself. It should agree with
     you on nine of ten [docs 20].
   - Its cost is recorded apart from the run's.
3. **You, reading**, when it matters. Every run writes one file to read: each failure in
   full, and a few passes. A run that settles a product question is not done until you
   have read it. Anthropic's own guide says the same: read the transcripts [docs 17].

**Three outcomes, not two.** Pass. Fail. And "could not tell": a timeout, a provider
error, a harness fault. The third is never counted as the model failing. The check's
PASS and CHECK become these three.

### How many runs make a result believable

A model does not do the same thing twice. So one run says almost nothing. What the
numbers allow [assumed: worked out with the standard formulas, the Wilson interval and
a two-sided test at the usual 5% and 80%]:

| Runs of one case | If it never fails, the true failure rate is still only known to be under | Enough to see a difference of |
| --- | --- | --- |
| 5 | 43% | always against never |
| 10 | 28% | |
| 20 | 16% | 50% against 5%, which needs about 14 a side |
| 30 | 11% | 90% against 60%, about 31 a side |
| 60 | 6% | 90% against 70%, about 62 a side |
| 200 | 2% | 90% against 80%, about 200 a side |

In plain words:

- **5 runs** is a smoke test.
- **20 runs** sees a large difference. The README's "20 runs of 20" was this size [code].
- **60 runs** sees a middling one.
- **A difference under ten points is not worth counting.** Read the transcripts
  instead.
- Every rate is printed with its interval. "18 of 20" reads as "90%, somewhere between
  70% and 97%".
- **A rare event cannot be ruled out by an eval.** "Does a gem ever loop" gets an upper
  bound from an eval. The answer comes from the record of real use.

**Comparing two things fairly** [docs 18, 20]:

- Run both on the same cases, in the same run, taking turns: A, B, A, B. A provider's
  speed drifts within the hour [docs 20], and its models can change behind the same
  name [assumed].
- So **an old run is a reference, never the control**. To compare with last month, run
  last month's wording again, beside the new one.
- For times, give the middle value and the slow tenth. The slow tenth needs about a
  hundred runs to mean anything [assumed].

**What it costs** [assumed: list prices and guessed sizes. The first real run replaces
this]. A scripted conversation of three or four model calls, with stored search and
faked speech:

| Model | One conversation | 4 cases, 20 runs each |
| --- | --- | --- |
| 6 Luna, Haiku 5.5 | about $0.001 | about $0.10 |
| 6.1 Terra | about $0.012 | about $1 |
| 6.1 Sol, Sonnet 5.5 | about $0.024 | about $2 |
| Opus 5.5 | about $0.05, more with its thinking | about $4 |

So the normal run is the two cheap models, at cents. The dear models are run on
purpose.

**The cost rule, built into the runner:**

1. Before it starts, it prints what it will run and what it expects to spend. The
   figure comes from the last recorded run of the same cases, or from a table of
   guesses when there is none.
2. It does nothing without `--yes`.
3. It stops when the spend so far passes a cap. The cap is twice the estimate unless
   you give one.
4. It has hard stops of its own, per conversation: on rounds in one reply, on time, on
   spend. The product has no cap on tool rounds, on purpose. The eval that asks whether
   a gem loops must not be the thing that burns the credit.

### Where results are kept

- **Each run gets a folder outside the repo**: `~/.cortex/evals/<date>-<name>/`. It
  holds every record line, every transcript, the verdicts and a manifest.
- **The manifest is what makes two runs comparable weeks apart.** It holds the commit,
  the full instructions each variant was sent, the model id the provider answered with,
  the hash of each case, the date of the price table, the number of runs and the spend.
- **A compare command** prints two runs side by side. It warns when they differ in
  more than the one thing being compared.
- **A run that settles something** gets a short summary in `evals/results/`, committed:
  the question, the numbers with their intervals, what you decided, and the name of the
  run's folder. Numbers and your verdict only. No transcript. A transcript can hold a
  gem's own instructions, and this repo is public.

### Keeping it out of the unit tests and out of CI

- `evals/` is its own folder, beside `tests/`. No file in it is named `test_*.py`.
- `pytest` is told to look only in `tests/`. Today it is not told where to look [code].
- An eval needs a key and `--yes`. A plain `uv run pytest` has neither.
- There is no CI in this repo today [code]. When there is, its default job is `pytest`,
  `ruff` and `pyright`. It holds no provider key, so it cannot run an eval.
- The runner's own code is unit-tested, with the scripted model: the driver, the
  checks, the cost sums, the intervals. Those tests check the harness. None checks a
  model.
- The stand-ins for speech and the clock now sit inside `tests/test_voice_conversation.py`,
  and the check imports them from there [code]. They move to one small module that
  both the tests and the evals use.

### The six questions that are waiting

| # | Question | Kind | Needs first | Rough cost [assumed] |
| --- | --- | --- | --- | --- |
| 1 | Three behaviours never run live | Behaviour | step 2 | $0.20 on the two cheap models. About $9 on all six |
| 2 | Does a gem ever loop | Behaviour, then watching | step 2, and step 1 for the watching | under $1 |
| 3 | Does a gem speak as the plain call does | Comparison | step 2 | under $1 on the cheap models |
| 4 | The first word against the size of what is sent | Speed | step 1 | $0.25 for two models. About $10 for all six |
| 5 | Thinking kept or dropped | Comparison | code that does not exist | not known |
| 6 | The three text questions | Comparison | code that does not exist | not known |

**1. The three behaviours.** Four cases, 20 runs each, per model. Three of the cases
are nearly in the check already [code].

- *A few words before a wait.* Exact: the round that searched said between one and
  twelve words first.
- *Carrying on after a sound.* Exact: the resumed reply does not begin with the first
  words of the original. Judge: does it carry on from the cut?
- *Telling a late result after answering something else.* Exact: the reply answers the
  new question and names the made-up fact from the stored result.
- A fourth: the user only says "hello?" while a search runs, and no second search
  starts.

**2. Looping.** Cases that tempt a loop: a search that finds nothing, a search that
always fails, a question with no answer. The check is rounds per reply, and the same
call made twice. Twenty clean runs only say "under 16%". So the real answer is the
record: rounds per reply, on every reply, in real use. That is the "watch first" the
plan already decided.

**3. A gem's wording.** The same cases, run four ways: no gem, a gem with no
instructions, one with short instructions, one with long ones. The checks are the
spoken-style ones the check already has: no markup, one or two sentences, never ending
in "…" [code]. Add words per reply, and the cases from question 1. All of them exact.
The gems' instructions here are made up for the eval, so they can be committed. To try
one of your real gems, the runner reads it from the database at run time. Its
instructions go only into the run's folder, never into the repo.

**4. Size and the first word.** This needs no conversation. The probe calls
`model.round()` directly.

- **Sizes**: about 2,000, 4,000, 16,000 and 64,000 tokens. The filler must look like
  what will really be there: tool descriptions, skill texts, memory notes. Not
  nonsense, because the question is also whether the model turns wordy.
- **Today's size too**, about 750 tokens, as the starting point. On OpenAI it can only
  be measured uncached: it is under the 1,024 minimum.
- **Cached and not**: a unique first line makes the provider read it all fresh. The
  same start sent again, within the cache's lifetime, is the cached case. The two take
  turns.
- **Measured**: time to the first text, words in the reply, and the cache counts, to
  prove which case each call really was.
- **Ten calls per cell to start.** Two cheap models first. The dear ones only at the
  sizes where the cheap ones showed something.
- **What is known already.** One study found the first word comes later by roughly 13
  to 22 seconds per million tokens on Claude models [docs 19]. If that held at small
  sizes, it would be a few tenths of a second at 16,000 tokens [assumed]. But it
  measured only above 50,000 tokens, on older models, and did not compare cached with
  not. Neither provider gives a number [docs 9, 11]. So the worry is reasonable and the
  answer is not published. This eval is how to get it.
- **Where caching starts**: 1,024 tokens on OpenAI [docs 9], 512 on the three Anthropic
  models used here [docs 11].

**5. Thinking kept or dropped.** Two things to know before designing it.

- **Only two of the six models think on every reply today**: 6.1 Sol and Opus 5.5.
  6 Luna and 6.1 Terra think a little only while a search is pending. Haiku 5.5 and
  Sonnet 5.5 do not think [code]. With thinking off there is nothing to keep.
- **Keeping thinking fights the call's own rules.** Anthropic ties a piece of thinking
  to the history as it stood. If an earlier turn is edited, later thinking is refused
  or dropped [docs 20]. A call cuts replies to what was heard and clears old search
  results. The code already says thinking sent back after a cut is refused [code]. I
  did not test that.

So this is a question for writing first, where the history only grows. The eval: one
long scripted conversation, thirty turns or more, with questions near the end that
depend on reasoning from the start. Run it both ways. Measure right answers, tokens,
the first word and cost. Sending thinking back has to be built first.

**6. Text.** `written_ent.py` takes one message at a time and refuses a second while it
writes [code]. So each question needs its behaviour built, as a switch the eval can
flip.

- *Is a second message a cut or an addition?* Both are built. Cases: the second message
  corrects the first, adds to it, or is about something else. Judge: does the final
  text answer what the user now wants? Count the tokens written and thrown away. Then
  you read them. This one is partly taste, and an eval can only inform it.
- *Is a cut reply kept as what was shown plus "…"?* Put a fact in the part that was
  not shown. Then ask about it. Exact: the gem does not behave as if you had read it.
  The check does this for speech already [code].
- *Does one context with speaking and writing confuse the gem?* A history with both
  kinds of turn. Exact: a spoken reply has no markup and is short. A written reply may
  be longer. An open point first: the instructions are fixed for the whole conversation
  [code], so how a gem is told which surface this reply is on is itself undecided.

---

## Part 3. Build or adopt

What each one is, what it would cost in complexity, and the verdict.

| Tool | What it is | Cost in complexity | Verdict |
| --- | --- | --- | --- |
| **OpenTelemetry**, the `gen_ai` conventions | Agreed names for a model call's facts: tokens in and out, cache reads, time to the first piece | All of it is marked "Development". It has moved to a repo of its own, which has no release yet. One token field was renamed on the way. It has no name for cost and nothing for speech calls [docs 1] | **Use the names, not the library.** The record's fields map one to one, so sending them later is a small exporter |
| **Langfuse**, self-hosted | Traces, cost, datasets and experiments | Seven containers. It asks for 4 cores and 16 GB. No small mode [docs 2] | No. It is heavier than Cortex |
| **Langfuse**, cloud | The same, hosted | Free up to 50,000 units a month, kept 30 days [docs 3] | No. The words leave the Mac |
| **Phoenix** | Traces and experiments, run locally | One process, one SQLite file under `~/.phoenix`. A large install, though it can run apart from the app. Usage analytics are on by default, with a switch to turn them off [docs 4] | **The one to adopt later**, if reading files stops being enough |
| **Logfire, Braintrust, LangSmith** | Hosted tracing and evals | Easy to start. Self-hosting is on their enterprise plans only [docs 5] | No. The words leave the Mac |
| **promptfoo** | An eval runner driven by a config file | Needs Node. Its Python hook takes one prompt and returns one text [docs 6]. A call with a clock and barge-in does not fit that [assumed] | No. Wrong shape |
| **Inspect** | An eval framework in Python, from the UK's AI Security Institute | The closest fit: repeats, intervals, a cost limit, a log viewer. About 40 direct dependencies. Its bridge sends the code's model calls through Inspect's own provider [docs 7]. Then the eval is not testing the adapters that ship [assumed] | Not now. **The one to look at again** if cases pass about fifty |

**Why build, in plain words.**

- What is needed is small: time a few moments, keep a few counts, append a line, run a
  script several times, count the passes. A few hundred lines [assumed].
- The hard part is not in any tool. It is driving this call, with its faked speech and
  its clock moved by hand. That already exists in the check [code].
- Every tool would have to be taught the call's own ideas: heard and not heard, a cut,
  a late result. None knows them.
- You said you want to learn. Thirty lines that work out an interval teach more than a
  dashboard.

**When to think again.** When someone else has to read the traces. When Cortex is
hosted for more than you. When cases pass about fifty. When you want a picture of one
reply's rounds and tools on a timeline. Then: export the record to Phoenix, or run the
cases under Inspect. The record does not change either way.

---

## The steps

### First step: the record. One day.

1. `RoundDone` carries the provider's counts, from both providers.
2. A record for one reply. It hands out the wrapped model and the wrapped toolbox, and
   it keeps time on a real clock.
3. The call adds the turn's end, the first sound, and how the reply ended. It also
   counts the characters sent to speech and the seconds of audio sent to listening.
   Both are cheap to count, and they are most of what a call costs.
4. A price file and the cost sum.
5. Lines are appended under `~/.cortex/records/`. A script prints one conversation.
6. Unit tests, with the scripted model: a reply writes the lines it should. A cut round
   is marked cut and estimated. The cost sum is right. No words are in any line.

Left out of this step: the `usage` event, and evals.

Why this first: every eval needs these numbers. Question 4 is nothing but these
numbers. And real calls start leaving a record from the first day.

### Second step: the eval runner, and the first live run.

1. The stand-ins move to a module that the tests and the evals both use.
2. `evals/`: the driver, taken from the check's `Talk`. The stored search. Four cases
   for question 1.
3. The runner: repeats, variants taking turns, the estimate, `--yes`, the cap, the hard
   stops, the three outcomes.
4. The run's folder with its manifest. The summary with intervals. The file to read.
5. **The first live run**, with your okay: question 1 on 6 Luna and Haiku 5.5, 20 runs.
   About 20 cents [assumed].
6. The check's fifteen scenarios are moved over as cases. Then the check script goes.

### After that, in this order

3. The `usage` event. A change to the contract, and a pull request in the app.
4. Question 4, the size probe.
5. The judge, and question 3.
6. Questions 5 and 6, when their code exists.
7. Real audio, a step at a time.

---

## Decisions for the owner

1. **Build small, or adopt a tool.** I recommend building. See Part 3.
2. **Live calls: numbers only, or words too.** I recommend numbers only, with a setting
   for words.
3. **Where records live.** I recommend files under `~/.cortex/`, not MongoDB and not
   the repo.
4. **Stored search results in behaviour evals.** I recommend yes, with one smoke case
   on the real search.
5. **The `usage` event.** Take the app's shape as it is, plus `cache_write_tokens` and
   `estimated`? And send it at the moments in the table?
6. **The spending rule.** A default cap per run. I suggest 50 cents. Which models are
   in the normal run. I suggest 6 Luna and Haiku 5.5.
7. **Your Cartesia plan**, so a credit has a price.
8. **Which model judges.** I suggest the cheap model of the other provider.
9. **Cases as Python functions.** Or a data format, at the cost of something to run it.
10. **What may be committed** to a public repo from evals. I recommend cases, wordings
    and number-only summaries.

## What I could not find out

- **Whether a cut-off model call is billed, and for how much.** Neither provider's
  documentation says. A reply from OpenAI's support on its forum says it is billed, and
  that the stream gives no counts [docs 8]. For Anthropic I found nothing.
- **Whether Cartesia bills characters that were sent and then cancelled.**
- **How much a cache hit shortens the wait for the first word.** No provider gives a
  number. No study I found measures it on these services.
- **The wait for the first word at sizes under 50,000 tokens.** Not published.
- **Whether thinking can be kept in a call at all**, given the cuts. The code says no.
  I did not test it.
- **The exact token size of today's instructions.** I counted characters. A true count
  needs a provider call, which this task ruled out.
- **Whether a model as judge agrees with you.** That needs your twenty labels.
- **Every figure marked [assumed].** Above all the costs. The first real run replaces
  them.
- **Whether OpenAI's `usage` names its output tokens as I expect.** The input side is
  used by the check [code]. The output side I have only from the SDK's types.

## Lines for `docs/darbar-notes.md`

I was asked not to edit that file. If this is accepted, these lines belong there.

Under "Decided":

- Evals and the record are our own small files. No tool yet.
- One record for live use and for evals. Made where the loop takes its model and tools.
- Live records hold numbers. Words only when a setting is on. Nothing leaves the Mac.
- An eval says its cost first, needs a yes, and stops at a cap.

Under "To try out with evals":

- The first word against the size of what the model is sent, cached and not, per model.
- A gem's wording against the plain call's.
- Thinking kept or dropped, in writing first.

Under "Deferred":

- Real audio in evals, three steps: real speech, real listening, the WebSocket.
- Sending the record to a tool. Phoenix first.

## Sources

Read on 17 November 2026.

1. OpenTelemetry's conventions for generative AI. The notice that they moved:
   <https://opentelemetry.io/docs/specs/semconv/gen-ai/>. Their new home:
   <https://github.com/open-telemetry/semantic-conventions-genai>
2. Langfuse, self-hosting: <https://langfuse.com/self-hosting>
3. Langfuse, pricing: <https://langfuse.com/pricing>
4. Phoenix: <https://github.com/Arize-ai/phoenix>. Its cost tracking:
   <https://arize.com/docs/phoenix/tracing/how-to-tracing/cost-tracking>
5. Pricing pages: <https://pydantic.dev/pricing>, <https://www.braintrust.dev/pricing>,
   <https://www.langchain.com/pricing>
6. promptfoo: <https://www.promptfoo.dev/docs/providers/python/>,
   <https://www.promptfoo.dev/docs/configuration/chat/>. It is now part of OpenAI:
   <https://www.promptfoo.dev/blog/promptfoo-joining-openai>
7. Inspect: <https://inspect.aisi.org.uk/agent-bridge.html>,
   <https://inspect.aisi.org.uk/setting-limits.html>,
   <https://inspect.aisi.org.uk/eval-logs.html>
8. OpenAI's forum, on usage for a cancelled stream:
   <https://community.openai.com/t/cancelling-stream-does-not-show-usage/1377768>
9. OpenAI, prompt caching: <https://developers.openai.com/api/docs/guides/prompt-caching>
10. OpenAI, pricing: <https://developers.openai.com/api/docs/pricing>
11. Anthropic, prompt caching:
    <https://platform.claude.com/docs/en/build-with-claude/prompt-caching>
12. Anthropic, pricing: <https://platform.claude.com/docs/en/about-claude/pricing>
13. Cartesia, how it bills: <https://docs.cartesia.ai/pricing.md>
14. Cartesia, plans: <https://cartesia.ai/pricing>
15. Parallel, search modes: <https://docs.parallel.ai/search/modes.md>
16. Pipecat's evals, which make the user's voice and write down the reply:
    <https://docs.pipecat.ai/pipecat/evals/overview>
17. Anthropic, "Demystifying evals for AI agents":
    <https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents>
18. Anthropic, "A statistical approach to model evaluations":
    <https://www.anthropic.com/research/statistical-approach-to-model-evals>. The paper:
    <https://arxiv.org/abs/2411.00640>
19. Epoch AI, on how the first word slows as the input grows:
    <https://epoch.ai/publications/long-context-latency-scaling-gpt-vs-claude>
20. Three of Anthropic's guides, read in the copies that ship with my tools, not on
    the web: on thinking that is kept, on measuring latency, and on checking an eval.
21. Anthropic, streaming: <https://platform.claude.com/docs/en/build-with-claude/streaming>
