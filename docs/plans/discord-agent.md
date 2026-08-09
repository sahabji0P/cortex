# Plan: Discord Agent

Status: **Deferred** — product/architecture plan only. Do not implement until this is
explicitly picked back up.
Last updated: 2026-07-05

## What this is

A Discord-facing Cortex agent that can be installed into a server, used privately, and
eventually join voice channels to speak and listen.

The Discord surface should be an adapter around the Cortex backend pipeline, not a new
brain. Cortex remains the model/tool orchestration layer; Discord handles identity,
channels, commands, and voice transport.

## Goals

- Install the agent into a Discord server.
- Support private usage through bot DMs or user-installable app contexts where Discord
  supports them.
- Provide slash-command chat first, then optional natural-message handling.
- Add voice in stages: join and speak first, listen/transcribe later.
- Keep all tokens and model/provider secrets in the backend or worker environment.

## Non-goals

- Do not automate a personal Discord user account or build a self-bot.
- Do not put model execution, Discord tokens, or long-running workers in `cortex-app`.
- Do not record voice channels silently; listening must be explicit and visible.

## Proposed phases

### 1. Text MVP

- Create a Discord application and bot.
- Add server install flow with the minimum bot/application command scopes.
- Implement slash commands such as `/ask`, `/chat`, `/reset`, and `/help`.
- Route prompts into the existing Cortex stream path and return the final answer in
  Discord.
- Store per-user or per-channel conversation state only after we decide the persistence
  model.

### 2. Private Mode

- Enable bot DMs where supported.
- Evaluate Discord user-install contexts for a more personal "use it from my account"
  feel without self-bot automation.
- Keep server-private usage simple: locked private text/voice channels are acceptable
  and predictable.

### 3. Voice Speak

- Add `/join`, `/leave`, and `/say`/`/ask-voice`.
- Run a separate Discord voice worker that joins a guild voice channel.
- Convert Cortex output to speech with a TTS provider.
- Encode/play audio through Discord voice.
- Keep FastAPI request handlers short; voice playback belongs in the worker.

### 4. Voice Listen

- Add `/voice start` and `/voice stop` so listening is explicit.
- Receive voice audio in the worker, map audio to Discord users, decode it, and send it
  to STT.
- Add voice activity detection / turn detection so the agent knows when to answer.
- Feed transcripts into Cortex and play TTS responses back into the voice channel.
- Mirror transcripts and agent replies into a paired private text channel for audit and
  recovery.

## Backend shape

- `DiscordAdapter`: verifies Discord requests, sends replies/followups, and owns REST
  client helpers.
- `DiscordInteractionRouter`: receives slash-command / interaction webhooks.
- `DiscordBotService`: maps Discord commands to Cortex prompts and response handling.
- `DiscordVoiceWorker`: long-running process for gateway/voice connection, TTS, STT,
  and audio playback.
- `DiscordConversationStore`: optional later layer for per-user/channel state,
  interaction IDs, preferences, and consent flags.

## Cortex integration

- Text commands can call the existing `LLMPipelineEntityClass` directly or go through a
  small service method that mirrors `/v1/cortex/listen`.
- Voice should use the same pipeline, with an input envelope that includes source
  metadata such as Discord guild, channel, user, and mode.
- When Gemini Interactions becomes the primary path, Discord conversation state can map
  naturally to `previous_interaction_id`.

## Frontend shape

The sibling `cortex-app` should stay a control plane:

- Configure Discord app/install status.
- Show connected servers/channels.
- Inspect Discord-originated runs and traces.
- Toggle voice/listening settings once the backend supports them.

It should not own Discord bot tokens, model provider secrets, voice loops, or durable
trace persistence.

## Security and privacy

- Use bot/OAuth APIs only; no self-bot behavior.
- Store Discord tokens in backend/worker secret config.
- Require explicit command-based opt-in for voice listening.
- Show clear in-channel status while the bot is listening.
- Keep retention configurable for transcripts and audio-derived text.
- Add admin controls for which servers/channels the bot may answer in.

## Open questions

- Which Discord Python library should own gateway and voice for Python 3.14 support?
- Do we want Discord interaction webhooks only, gateway events only, or a hybrid?
- Which TTS/STT providers fit latency and cost best?
- Should long-term memory be per user, per private channel, or per server workspace?
- Should Discord voice use Gemini Live directly later, or stay STT -> Cortex -> TTS for
  observability and tool routing?

## Not doing now

This is parked until the Discord feature is explicitly prioritized. The current blocker
work remains Gemini Interactions and the Cortex backend stream/runtime contracts.
