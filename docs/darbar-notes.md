# Darbar notes

Things that came up while planning. One line each, as a reminder. Started 2026-11-16.

## Words

- Gem: one of your assistants. Never "bot" or "agent".
- Darbar: the common place, where gems meet and talk. Also the part every gem is told.
- Home: a gem's own private place (its computer and files).
- Bhandar: a gem's library of files.
- Samvaad (proposed): the one ongoing conversation with a gem, by call or by chat.
- Gyan Kosh (proposed): memory.
- Granth (proposed): a long document a gem writes, kept in its Bhandar.
- Vichar Dhara (proposed): the setting that carries a gem's thinking forward.
- Connection: a tool server (MCP) the user has added. Still "for now".
- No name yet: skills, the vault.
- Cortex: the name of the whole project, which holds one or many gems. Never an assistant.

## Decided

- A gem is one entity with one context. Call or chat, it is the same context. No sessions. Built: a call starts from the stored conversation and stores its turns.
- Every gem has both call and chat.
- Always a gem: there is no call without one. A call answers with the gem's model.
- How a call behaves is closed, until we find something that makes it better.
- One WebSocket once you are logged in. Calls, chat and events all go over it.
- Calls go over the standing socket only. The old call socket and the test page are gone: the app is the client.
- It goes both ways: a gem can call or message you.
- Call and chat are a medium between any two: you and a gem, or gem and gem.
- One agentic loop for call, chat and workers. Only the config differs.
- A gem is defined by a GemConfig: in the database, treated like a config file.
- No cap on tool rounds in one reply. No fuse either: watch first, build if it loops.
- Before a wait, the gem says a few words first. Same in chat as in a call.
- Instructions and skills are plainly written. Nothing hard-coded.
- The list of gems changes, so it is not in the fixed instructions.
- What a gem is told is two parts: the Darbar text (the environment, same for every gem) and the gem's own instructions.
- There is no other Darbar text: it is the environment instruction, and it stays editable.
- The instructions do not depend on call or chat, or on the tools offered.
- A new gem's instructions come filled in with the call and writing style rules. The user may change or remove them.
- A line says when a call starts and when it ends: before the first turn of every call, and before the first written turn after a call. Written when the next turn needs it.
- Every stored item says its mode, call or chat.
- One rule for what the user says, in a call and in writing: it is stored the moment it is said, as a message of its own. Two messages are never joined.
- One writer for a gem at a time, a call or a written reply. Chat to a gem in a call gets an error.
- One set of background calls per gem: their results told in the call if one is open, in writing otherwise.
- A late result waiting when the user's turn ends goes in ahead of their words: one reply answers and tells it. Live, it was told in 14 of 20 runs on 6 Luna, 9 of 20 on Haiku 5.5; else not in that reply.
- Product questions are settled by trying them out with proper evals, not blindly.
- Tools are the same in a call and in chat: the same tools, run the same way, by one copy of the code.
- A tool call still running at a cut carries on in both. cancel_tool_call stops one, for any tool.
- Nothing is cleared from the history by itself, in a call or in chat. Keep it simple.
- Reset gem: the user resets a gem and it starts again, knowing nothing. Built for its conversation. The log keeps all of it.
- A reset is of everything a gem knows: when Gyan Kosh comes, a reset clears that too.
- Reset asks the user to confirm first. In the app: the side panel and the gem's settings.
- No branches: one line of conversation, by design.
- A stored result says what it is by a mark, never by its wording.
- In a call the app shows a reply's words as they are heard: each word is sent with where it starts in the audio. The model's text is not sent early. Chat is unchanged.
- A connection is an MCP server reached by address. Its tools are offered next to web search, in a call and in chat.
- Who may use a connection is said on the connection: all gems or some. One tool can be narrowed further. Built.
- A new connection is open to no gem until you say which.
- A gem is offered a copy of a server's tools, made when you ask. A server cannot change them behind your back.
- Closing a tool to a gem holds at once, even in an open call: every call of it reads the connection again.
- A gem's own `tools` are the built-in ones only.
- A connection can sign in through the browser (OAuth): add it by address only, and Cortex gets the pass itself. Built.
- The browser comes back to a route on the backend, on localhost: the backend runs on your Mac. Rethink when it is deployed or runs on another machine.
- A pass is renewed by itself when it can. One that cannot is dropped when refused, the app is told, and the gem is told in plain words.
- A tool call never waits on a browser.
- Fixed headers stay, for servers that take a key.
- A connection carries its server's own icons and website, read with its tools. The backend fetches each icon and sends it as data: the app never fetches one. Built.
- A connection is deferred unless you turn it off: one flag, on by default. Its tools are found with our own `tool_search` and loaded by each provider's deferred loading, at the end of the context, so the cache holds. Built.
- OpenAI can go through a proxy (CLIProxyAPI on a ChatGPT login) by its own two settings. The API key stays. With both set up, the user switches between them in the app; the choice is kept.
- Measured 2026-11-19: with a real-sized request (about 3,400 tokens) a reply through the proxy on a free plan starts about 5.4 s in, against 1.6 s with the API key. Why is not known. So the API key is the default.
- The proxy's ChatGPT account is changed from the app: a sign-in in the browser, and the new account replaces the old logins on the proxy.
- Trajectory: the name for seeing everything a gem did. In the app it is a channel of each gem, beside its chat. Home shows totals.
- A gem has one log. Everything that happens with it is written there, in order, beside what it remembers. Built.
- The log is the base. A new thing a gem does is a new kind of entry in it, never a store of its own.
- Nothing is hidden in it: every request, what it was sent, its tokens, its cost, every tool call, every change of model, instructions or tools.
- One log, one shape of entry: seq, at, kind, turn, and its own fields. Each thing is written once, as the kind it is, at the moment it is final.
- What the gem remembers is five kinds of entry: user.said, note.written, gem.said, tool.called, tool.result. Its conversation is those, in order, since the latest reset.
- A request holds a count only: it was sent every remembered entry before it, since the latest reset.
- What a reply produced before a tool call joins the history when it is sent back to the model, before the request that carries it.
- A cut reply shows what was heard. Its tokens and cost are the whole of what the provider charged. Cut while the model was still producing, no count is read yet: shown as unknown.
- Cost is worked out by us from a price table kept by hand. No provider reports one. Unknown is shown as unknown, never as nothing.
- Prices: all six models, read from OpenAI's and Anthropic's price pages on 2026-11-20. A long request pays the higher prices where the page says so.
- Through the ChatGPT proxy the cost shown is what the API key would have paid.
- A gem is never told which model it runs on. A model change is in the log only.
- A tool call still running when its reply is over goes to the background. It is read start to end by its call_id: started, backgrounded, done, then delivered, cancelled or lost.
- Writing what happened never holds a reply up and never fails one. What is remembered must be stored: a reply or a call that cannot store it fails, and it is dropped, never stored later.
- No plugin system. The log is the one place to listen: the next thing that looks at a gem reads it.
- The date and time are a line of ours, not part of the instructions. The instructions change only when the user edits something. Built 2026-11-20.
- The line gives the date, the time and the user's time zone. The app sends the zone on chat.send and call.start. With no zone, or one that cannot be read, the time is in UTC and the line says so.
- When it is written: as a call opens, and in writing before a message when there is none yet, the last is over ten minutes old, or the date has changed.
- The Darbar text does not mention the time line: the line explains itself.
- To watch: a zone changed by the user may show only once the ten minutes are up.

## Base (next, order not fixed, no rush)

- The log: a provider library's own retries, each written as a request of its own. Today up to two are made unseen.
- The log: listening and speaking as entries, with their times and cost (speech-to-text is charged by the second of audio sent, speech by the character).
- The log: totals by gem, by day and by service, for the app's home.
- The log: the tokens of a reply cut while the model was still producing. Anthropic says what it read at the start of a reply. Whether OpenAI says anything before the end is to be checked.
- Our lines sent the same way on every model: a user message in a tag, the developer role kept for instructions. The line that explains the tag in the instructions for every model. A change to what the model is sent, so on its own.

- GemConfig: skills, permissions and sandbox still to add. Instructions, model and tools are built.
- Skills, on the tool registry (`tools.py`).
- A sound before the greeting's first word leaves the call silent. Small fix.
- A reply that begins with "..." can be judged unheard and dropped from memory: the speech service gives its first word without the dots. To be fixed.
- The code's own names still say late results (`late_results.py`, `LateResults`). The log says background.
- A log written before the one shape (entries of kind `item`) is not read. It is cleared, not carried over.

## Deferred

- Gyan Kosh: what context a gem carries, and how much. The main work, and the important one.
- Homes and the Darbar are part of the memory design too: what is private to a gem, what is common.
- Proactivity: gems calling or messaging first, routines. Comes with memory.
- Gem to gem samvaad, and several gems in one call.
- Workers: a worker returns to its parent gem. A shared worker that is triggered is a new idea.
- Vault: secrets per gem, and shared ones. The model never sees a secret.
- Vault: a gem without a secret asks the gem that has it to do the job. The secret is not handed over.
- Permissions: allow, ask or block, per gem and tool. Built so far: which gems, per connection and per tool. No "ask".
- Connections by local command (stdio): not until there is a sign-in. A route that starts a command is too open.
- Connection headers, passes and registrations are kept in the database as given. They move to the vault.
- A pass running out: a badge in the app only. No pop-up, no notification.
- Sign-in servers that do not let Cortex register itself, and scope step-up on a 403: not built.
- Connections over SSE: not built.
- A connection's pictures and sounds are not passed on to the model, only text.
- A server telling us its tools changed: not listened to. You refresh.
- A connection document that cannot be read stops every gem's reply. Skip it and say so instead.
- A deleted connection's key is free again: a new one of the same name takes over its tools' names in old conversations.
- Sandbox: what a tool can reach even when it is allowed.
- Hooks: our own code run at fixed moments of the loop.
- Homes: each gem's own computer, hosted so it works when the Mac is off.
- A gem using your PC, with permission.
- Granth as a page, like Notion or Claude artifacts. Doable in the app.
- OpenUI-style rendering: the open one. Find out what GPUI can draw.
- Vichar Dhara toggle per gem: keep the thinking and pass it back, or drop it.
- Config: how you control and customise a gem.
- Characters: animated raja and rani types, GIF or Rive.
- Cost of speech and search, in the log. Model requests are costed.
- Which mode each message was in, in the history route's answer: on hold.
- A reply that plays to its end is stored only when the user next speaks or the call ends.
- A long conversation is sent whole at the start of every call. Trimming comes with Gyan Kosh.
- Compaction: a summary in place of the older part. Next, with the caching work.
- Rewind to a turn, instead of resetting everything: put off.

## Learned from Docker Agent (read 2026-11-16, config version 16)

- One file defines a whole team. Models, tool servers, skills and budgets are named once at the top and shared.
- An agent is: description, instruction, model, toolsets, sub_agents, handoffs, skills, hooks, limits.
- The config has a version number and an upgrade step for each old version.
- No cap on tool rounds by default. It stops only on the same tool calls repeated 5 times.
- Tool calls in one reply run in parallel.
- A new message during a run is "steer": it joins at the next step and does not cut the run.
- A worker gets only its task, not the parent's history. Only its last message comes back.
- No worker lives on or is shared. Every delegation is a new one.
- A handoff is different: the next agent takes over the same conversation.
- Permission is checked per tool call: allow, ask or deny, by tool name and arguments. Deny always wins.
- Four modes: strict, balanced, restricted, autonomous. Unattended runs turn an ask into a deny.
- Hooks: about 30 moments. Some can add text for the model, some can rewrite, some can block.
- Skills: a list of names and descriptions in the prompt, the body read by a tool.
- Memory is only a tool there (add, search, update, delete). Thin.
- Thinking is kept and sent back by default.
- Compaction at 90% of the window: a summary, with the latest part kept as it is.
- Its sandbox is not its own: the whole agent runs in a Docker Sandboxes VM.
- In the VM: its folder is read-write, the rest read-only, network closed except an allowlist.
- Keys never enter the VM. A proxy adds the real key to requests for one domain.
- Outside the VM there is no real wall: the shell sees every key on the machine.
- One sandbox for the whole team, not one per agent. We want one home per gem.
- Tool output is scrubbed for secrets by pattern. It can miss.
- Optional git snapshots of the work folder, for undo.

## To try out with evals

- Chat with no stop button: a new message cuts the reply, like barge-in.
- Chat: is a second message a cut, or an addition to the first?
- Chat: is a cut reply kept as what was shown plus "…", like a call?
- Sizes: short in a call, medium in chat, long as a granth.
- One context with both speaking and writing in it: does the gem keep to the right one?
- No cap on tool rounds: does a gem ever loop?
- Call: three things are now asked of the model and not yet run live: a few words before a wait, carrying on after a sound, telling a late result after answering.
- One instruction text for call and chat: does a gem keep to the call rules in a call and the writing rules in writing?
- Connections: no run with a real model yet. Does each provider take every schema a server lists?
- Many tools in a call: how much later is the first word?
- A deferred tool's first use in a call costs one more model round. How long is that wait before the first word?
- A connection's result is cut at 20,000 characters. Is that the right number?
