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
| `/v1/darbar` | `src/darbar.rs` | The Darbar text every gem is told first |
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
- **What a gem is told is two parts, the same in a call and in writing.** The Darbar
  text, which by default says how things are here (calls, writing, a cut reply, a late
  tool result), and the gem's own instructions. Both are the user's to change.
- **A call is always with a gem.** `call.start` names the gem, and may say who speaks
  first. The gem's config says which model answers: a call cannot ask for another.
- **A gem has one conversation, called or written to.** A call starts from what is
  stored and stores its turns as they settle; the history route shows them as `user`
  and `gem` messages. One writer at a time: while a call holds a gem, a `chat.send` to it
  gets `chat.error` and another `call.start` gets `call.refused`, both with "the gem is in
  a call", and a `call.start` cuts a reply being written to it. A call whose turns cannot
  be stored ends in `call.failed`. A message whose reply never got through joins the next
  one, and they are stored as one user message.
- **The app expects more than this backend sends.** It has screens for cost (it looks
  for a `usage` event that does not exist yet) and a library of files per gem. Those
  are marked "coming soon" or show sample data in the app. When one is built here, tell
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
