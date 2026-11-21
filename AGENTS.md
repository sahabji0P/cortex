# Notes for agents working on Cortex

Read `README.md` first. It explains the voice cascade, the rules for what the model
remembers, how to run the backend, and how the code is layered.

## This backend has a front end

The desktop app for this backend lives in its own repository:

- Code: `../cortex-desktop` on this machine, <https://github.com/sahabji0P/cortex-desktop>
- What it is: a native macOS app written in Rust with GPUI. It shows a set of "gems"
  (the user's name for their assistants), and holds calls and written chats with them.

The app talks to this backend in these places. Each is mirrored by hand in the app, so
a change here needs a matching change there:

| Here | In the app | What it carries |
| --- | --- | --- |
| The standing socket `/v1/link` (`app/routers/link`; its frames in `docs/chat-contract.md`) | `src/link.rs`, `src/chat.rs`, `src/voice.rs`, `src/audio.rs` | Written chat, and calls: `call.start` and its settings, the call's audio and how it is played, `call.playback`, and the frames a call ends with |
| `VoiceEvent` in `app/entities/voice_ent.py` | `src/protocol.rs` | A call's events, sent on the link with `call.` put before their type |
| `/v1/gems` (`app/routers/gems`, `GemConfig` in `app/entities/gem_ent.py`) | `src/gem_routes.rs` | The gems and their configs |
| `GET /v1/gems/{id}/chat` | `src/chat.rs` | A gem's conversation so far, a call's turns included |
| `GET /v1/gems/{id}/log`, `GET /v1/gems/{id}/log/{seq}`, `GET /v1/gems/{id}/log/{seq}/sent`, and the frames `log.watch`, `log.unwatch` and `log.entry` on the link (the entries are written down in `app/entities/gem_log.py`) | being built: the trajectory | A gem's log: every turn, every request to the model with its tokens and cost, every tool call, every change of model, instructions or tools, and what the gem remembers, as entries of one shape in the order they happened |
| `/v1/darbar` | `src/darbar.rs` | The Darbar text every gem is told first |
| `/v1/connections` (`app/routers/connections`, `Connection` in `app/entities/connection_ent.py`), and the frame `connection.changed` on the link | not built yet | The tool servers gems can use, their tools, which gem may use which, whether a gem finds its tools by searching (`deferred`), and each one's sign-in: its state and when its pass runs out, a route that gives the address to open in the browser, and a frame when a sign-in changes |
| `/v1/providers/openai` | being built | How OpenAI is reached, with the API key or through a proxy on a ChatGPT login: the way in use, which ways are set up, and the user's switch. Also the proxy's ChatGPT accounts (email, plan, whether usable), and a sign-in that replaces them: a route that gives the address to open in the browser, and one the app asks until it says `ok` or `error` |
| `GET /v1/voice/models` | `src/models.rs` | The models a call can be held with |

What that means in practice:

- **Adding or renaming a frame, a field of a frame, a route, or a field of a gem or a
  model** is a change to the contract. Say so in the pull request, and link the app's
  pull request that follows it.
- **`docs/chat-contract.md` is the reference for a client.** It holds every frame of the
  standing socket, and the rules a client plays a call's audio by: mic frames of about
  100 ms at 16 kHz, speech at 24 kHz played back to back, everything queued dropped on
  `call.interrupt`, and the sound device's delay reported in `call.playback`. The backend
  keeps its own copy of the app's playback schedule and judges interruptions by it, so
  if those rules change, `src/audio.rs` must too.
- **A gem's config lives here.** The app shows and saves all of it: name, description,
  picture, instructions, model, whether it speaks first, and its tools. Its voice and
  speed are kept but no call uses them yet, so the app shows them as "coming soon".
  A gem made with blank instructions is given the default ones (how to talk in a call
  and how to write), and the app shows what comes back.
- **A gem's `tools` are the built-in ones only.** A connection's tools are not named
  on the gem. Who may use them is said on the connection: the gems it is open to, and
  for one tool a narrower list. A new connection is open to no gem. The app has no
  screen for connections yet: when it gets one, it is the only place this is changed.
  A header's value is never sent to the app, only its name.
- **A connection can sign in through the browser.** Added with only an address, to a
  server that asks for a sign-in, it is kept with no tools and `sign_in.state`
  `"sign_in_needed"`. The app asks `POST /v1/connections/{id}/sign-in` for an address,
  opens it in the browser, and the browser comes back to this backend, not to the app
  (on localhost: the backend runs on the user's Mac). The link then says
  `connection.changed` with the connection's `id`, and the app reads it again. The same
  frame comes when a pass is refused and cannot be renewed. When a pass is expired or
  close to it, the app shows a badge only: when `sign_in.state` is `"sign_in_needed"`, or
  when `sign_in.expires_at` (null when unknown) is near and `sign_in.renews` is false. A
  pass that renews by itself gets no badge for its time. The pass never reaches the app.
- **What a gem is told is two parts, the same in a call and in writing.** The Darbar
  text, which by default says how things are here (calls, writing, a cut reply, a late
  tool result), and the gem's own instructions. Both are the user's to change.
- **A call is always with a gem.** `call.start` names the gem, and may say who speaks
  first. The gem's config says which model answers: a call cannot ask for another.
- **A gem has one conversation, called or written to.** A call starts from what is
  stored and stores what it adds as it goes; the history route shows them as `user`
  and `gem` messages. One writer at a time: while a call holds a gem, a `chat.send` to it
  gets `chat.error` and another `call.start` gets `call.refused`, both with "the gem is in
  a call", and a `call.start` cuts a reply being written to it. A call whose turns cannot
  be stored ends in `call.failed`. Everything is stored the moment it is final: a
  message when it is sent, whatever becomes of its reply, so one whose reply never got
  through is a user message of its own, and the next one follows it.
- **A gem has a log, and it is the base for anything that looks at what a gem did.**
  Everything that happens with a gem is written down in one list, in order, each thing
  once, as the kind of entry it is. Five kinds (`user.said`, `note.written`,
  `gem.said`, `tool.called`, `tool.result`) are what the gem remembers, and its
  conversation is those entries: `docs/chat-contract.md` says every kind and its fields.
  A new thing a gem does (a new step of the loop, a new paid service) is a new kind of
  entry there, written through `gem_log.py`, and never a store of its own. Adding a
  kind, or a field of one, is a change to the contract. Writing what happened never
  holds a reply up and never fails one. What the gem remembers must be stored: a reply
  or a call that cannot store it fails. Nothing in the log but what the gem remembers
  is ever sent to a model.
- **Cost comes from the log.** No provider reports a cost, so the backend works it out
  from the tokens and its own price table (`app/entities/prices.py`), which is kept by
  hand. A cost is null when it is not known, never zero. The `usage` event the app
  once waited for is never sent: the app's cost screen is to be rebuilt on the log.
- **The app expects more than this backend sends.** It has a library of files per gem,
  marked "coming soon" or showing sample data in the app. When it is built here, tell
  the app.

## Where the project is going

`docs/darbar-notes.md` holds the plan in short lines: the project's own words (gem,
Darbar, Bhandar and the rest), what is decided, what comes next, what is deferred, and
what is still to be tried out. Read it before designing anything new, and use its words.
When something new comes up or is put off, add one line there. Keep it to simple lines,
no long paragraphs.

`docs/voice-spec.md` says in plain words what a call does and what the model is sent:
the pipeline, interruption, tool calls, the instructions in full, and the known gaps.
`docs/voice-context.excalidraw` is the same thing as a picture. When how a call behaves
changes, change both.

## Commits

Never commit a piece of work as one commit, and not as "the code" plus "the docs" either.
Split it into small commits, one for each step a reader would follow:

- Go from the bottom up, in the order of the layers: a setting, an adapter, an entity,
  the routes, the link's frames, then the tests, then the docs.
- A test goes with the code it tests. A test file that needs every layer at once is a
  commit of its own, after the last layer.
- The docs are split by what they are: the contract and this file, the README and the
  spec, the notes.
- Each commit passes `uv run pytest` and `uv run ruff check app tests` by itself. Check
  that at each commit, not only at the last one.
- A commit that changes the contract says "Contract change:" in its message, and what.
- Read the whole diff before the first commit and plan the groups. If the work is already
  in one commit and not pushed, undo it and commit it again in groups.
