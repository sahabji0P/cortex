# HeyClicky: current public product

Verified against the live HeyClicky website on 1 August 2026.

## Scope and confidence

This note records what HeyClicky publicly claims and demonstrates today. It does not infer the current private implementation from the old `farzaa/clicky` repository. That repository's README says the existing code remains open source while newer work is private; use it only as a historical prototype and source of interaction ideas.

Primary sources:

- Current product site and FAQ: https://www.heyclicky.com/
- Current direct-download release channel: https://github.com/farzaa/clicky-releases/releases/latest/download/HeyClicky.dmg
- Historical open-source prototype: https://github.com/farzaa/clicky
- Privacy policy linked by the product site: https://www.heyclicky.com/privacy-policy

## Product definition

HeyClicky describes itself as “an AI buddy that lives on your Mac.” Its product thesis is that access to frontier models is becoming common, while the missing piece is an interface that escapes chat boxes and terminals. The interface combines voice, explicitly invoked screen context, spatial guidance, dictation, and delegated agents.

The current product has two clearly separated modes:

1. **Talk** is the conversational and teaching surface. The user presses a hotkey, speaks, and lets HeyClicky use the visible screen as context. It responds aloud, walks the user through the next step, and can point or draw directly on the screen.
2. **Agents** is the task-execution surface. The user can say “heyclicky agent” and delegate work by voice, without opening a terminal. The site specifically advertises Gmail and Notion connections and demonstrates an agent working with Google Sheets.

This distinction is also commercial: Talk and agent messages are metered separately.

## Current feature inventory

### Voice-first invocation and conversation

- Global hotkey invocation on macOS.
- Spoken questions and spoken guidance.
- A Talk mode positioned for ongoing conversation and learning.
- Voice invocation of delegated agents via “heyclicky agent.”
- Dictation is a separately named product capability and plan entitlement.
- The site demos the experience across tools rather than inside a dedicated chat window.

### Screen context and spatial interaction

- Captures the screen only after explicit hotkey invocation, according to the FAQ.
- Uses anything visible on screen as context; the product claims no per-app plugin is required for this basic visual understanding.
- Walks the user through what to do next when they are stuck.
- Draws or points directly on the screen to teach and direct attention.
- The current site has a dedicated `clicky-spatial.mp4` demo, making spatial context a first-class marketed feature rather than a minor cursor effect.
- Marketed examples include FL Studio, Claude Code, Preview, and learning unfamiliar creative software.

### Agent execution

- Spawns AI agents using voice, with no terminal required.
- Agents “actually do tasks,” in contrast with Talk answering or teaching.
- Advertised connections include Gmail and Notion.
- The website demonstrates an agent in a Google Sheets context and uses mail as another recurring visual example.
- Agent usage pauses when the monthly agent-message allowance is exhausted; Talk continues.

The public site does not define the current agent runtime, tool protocol, model routing, confirmation policy, or exact boundaries of Gmail/Notion access. Those remain implementation unknowns.

### Cross-application teaching

- Claims compatibility with anything visible on screen.
- Teaches the user how to use arbitrary tools.
- Combines explanation with visual screen annotations.
- The positioning is “finally do the thing”: reduce the gap between asking for help and acting inside the tool already open.

### Privacy and retained context

Public FAQ claims:

- HeyClicky does not continuously watch the screen.
- Screen access occurs when the user presses the hotkey.
- Screenshots are never stored.
- Basic text summaries are retained to provide conversational context.
- Users can delete their account and associated data in Settings.

These are product claims, not independently verified data-flow guarantees. Before building an analogue, Cortex should define precisely what crosses the network, what is retained, retention duration, deletion semantics, and whether screen-derived text enters summaries.

### Platforms and distribution

- macOS only today.
- Requires macOS Sonoma 14.2 or newer.
- Windows is represented by a waitlist, not a released client.
- Distributed as a signed-looking direct DMG download through the `farzaa/clicky-releases` GitHub release channel; the website does not route users through the Mac App Store.
- The site claims more than 25,000 users.

### Pricing and usage limits

Monthly prices shown on 1 August 2026:

| Plan | Price | Talk | Dictation | Agent messages |
| --- | ---: | --- | --- | ---: |
| Free | $0 | 25 messages/month | “plenty” | 25/month |
| Pro | $20/month | unlimited | unlimited | 150/month |
| Max | $100/month | unlimited | unlimited | 1,000/month |

- Annual billing is advertised at 20% off.
- Plans are month-to-month and can be cancelled while retaining access through the paid period.
- A maker offer gives 50% off the first Pro month after showing a qualifying app, YouTube channel, or project.
- The FAQ also advertises a student offer: first month free and 50% off Pro for new subscribers after school-email verification.

## Website demo assets observed

The live page currently names these product-oriented demos:

- `heyclicky-draw.mp4`
- `usecase.mp4`
- `it-draws-too-omg.mp4`
- `daddyshome.mp4`
- `nohandstricklol.mp4`
- `hello.mp4`
- `clicky-fl.mp4`
- `clicky-spatial.mp4`
- `clicky-agent.mp4`

The last three align directly with the site's three core demonstrations: teaching inside FL Studio, spatial screen context, and voice-spawned agents.

## What changed from the public open-source prototype

The historical repository documents a relatively direct pipeline: push-to-talk audio to AssemblyAI, transcript plus screenshot to Claude, and ElevenLabs speech output. That is not a reliable description of today's product.

The current public product has expanded from a voice/screenshot tutor into a three-part interface:

1. **Perception:** explicitly invoked screen understanding plus spatial user context.
2. **Guidance:** voice conversation, teaching, drawing, and pointing in the active tool.
3. **Execution:** voice-spawned agents with connected services and separately metered usage.

Current model providers, transcription/TTS providers, orchestration architecture, and native-helper design cannot be established from the website. Keep the July binary teardown as a point-in-time implementation observation, but do not let either that binary or the old GitHub repository override current product behavior.

## Product takeaways for Cortex discussion

- The important unit is not “voice chat”; it is a fast loop from **intent + visible context** to either **guidance** or **delegated action**.
- Talk and Agents should remain legible, behaviorally distinct modes even if they share conversation history.
- Spatial context is bidirectional: the user can indicate a region, and the assistant can return guidance anchored to the screen.
- The hotkey is also the privacy boundary and the moment of context capture.
- The broad “works with any app” promise comes from screen-level perception; reliable action should still prefer structured integrations and reserve computer use for last-mile UI work.
- Metering agents separately communicates that delegated execution is more expensive than conversation and allows Talk to remain available after agent limits are reached.

