# Notes for agents working on Cortex

Read `README.md` first. It explains the voice cascade, the rules for what the model
remembers, how to run the backend, and how the code is layered.

## This backend has a front end

The desktop app for this backend lives in its own repository:

- Code: `../cortex-desktop` on this machine, <https://github.com/sahabji0P/cortex-desktop>
- What it is: a native macOS app written in Rust with GPUI. It shows a set of "gems"
  (the user's name for their assistants) and holds voice calls with them.

The app talks to this backend in three places. Each is mirrored by hand in the app, so
a change here needs a matching change there:

| Here | In the app | What it carries |
| --- | --- | --- |
| `VoiceEvent` in `app/entities/voice_ent.py` | `src/protocol.rs` | The JSON events sent down the call's WebSocket |
| The WebSocket at `/v1/voice/converse` (`app/routers/voice`) | `src/voice.rs`, `src/audio.rs` | Audio formats, the `?model=` and `?first=` query, the `playback` delay frame, close codes 1008 and 1011 |
| `GET /v1/voice/models` | `src/models.rs` | The models a call can be held with |

What that means in practice:

- **Adding or renaming an event, a query setting, or a field of a model** is a change to
  the contract. Say so in the pull request, and link the app's pull request that
  follows it.
- **The test page is the reference client.** The app's audio code copies
  `app/routers/voice/test_page.html`: mic frames of about 100 ms at 16 kHz, speech at
  24 kHz played back to back, everything queued dropped on `interrupt`, and the sound
  device's delay reported in a `playback` frame. If the test page's behaviour changes,
  the app's must too.
- **A gem sends two settings today**: which model answers, and whether the gem speaks
  first. The app keeps more per gem (instructions, voice, speed, web search) and shows
  them as "coming soon". They have nowhere to go until this backend accepts them.
- **The app expects more than this backend sends.** It has screens for cost (it looks
  for a `usage` event that does not exist yet), text chat, and a library of files per
  gem. Those are marked "coming soon" in the app. When one is built here, tell the app.

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
