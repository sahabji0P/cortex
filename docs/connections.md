# Connections

A connection is a tool server the user has added: an [MCP](https://modelcontextprotocol.io)
server reached by its address, over Streamable HTTP. Its tools are offered to the gems
it is open to, next to web search, in a call and in writing, and they are run the same
way: by the one loop, carrying on in the background when a reply is cut.

This page says how the backend treats a connection. Its routes, the shape the app reads
and the `connection.changed` frame are in
[the contract](chat-contract.md#connections-v1connections). The code is
[`app/entities/connection_ent.py`](../app/entities/connection_ent.py), and the server is
reached through the MCP SDK in [`app/adapters/mcp_adp.py`](../app/adapters/mcp_adp.py).

## What is kept

Connections are kept in the `Connections` collection, one document each:

| Field | Meaning |
| --- | --- |
| `name` | what the user calls it: required, trimmed, at most 100 characters |
| `key` | the start of its tools' names as the model is told them: `{key}__{tool}`. Made from the name when the connection is added (`Issue Tracker` gives `issue_tracker`), at most 20 characters, never the same for two, and never changed after, since the stored conversations name the tools by it |
| `url` | the server's address, `http://` or `https://`, with no user name or password in it. At most 2,000 characters |
| `headers` | sent with every request to the server, such as `Authorization: Bearer ...`. At most 20. Kept as they are given, and never sent back to a client: a client is told their names only |
| `oauth` | for a connection that signs in through the browser: how its server is signed in to, as the server's metadata says (the sign-in server, its addresses, the scope asked for). Null for one that does not sign in |
| `oauth_client` | this backend's registration as a client of the sign-in server, with its secret if it was given one, and the address the browser was registered to come back to |
| `oauth_tokens` | the pass: what lets us in, what renews it, and when it runs out (null when the sign-in server did not say). Null when a sign-in is needed |
| `enabled` | off, no gem is offered its tools, and who it was open to is kept. Default on |
| `deferred` | on, its tools are not sent with every request: a gem finds and loads them with `tool_search` when it needs one (see [Deferred tools](#deferred-tools)). Off, all of them are sent in full with every request. Default on |
| `gems` | the gems it is open to: `"all"`, or a list of gem ids. **A new connection is open to none** until the user says which |
| `tools` | a copy of the tools the server listed when it was last asked: each one's name, title, description and schema |
| `icons`, `website_url` | how the server shows itself, as it stated when its tools were last copied (see [The server's icons](#the-servers-icons)). Empty and null when it stated nothing |
| limits | for one tool, the gems it is open to. A limit only narrows: the tool is offered to a gem when the connection is open to the gem and the limit names it too. A limit that names no gem turns the tool off for everyone. A tool with no limit is open to whoever the connection is |

## Who may use what

**It is said on the connection, not on the gem.** A gem's own `tools` name the built-in
tools only. Which gems may use a connection, and for one tool a narrower list, is the
whole permission layer for now. There is no "ask first" yet (see the
[notes](darbar-notes.md)). It is checked twice:

- **When a reply or a call starts**, it decides which tools the gem is told of. A written
  reply reads it every time, so a change applies from the next message. A call reads it
  once, when it opens, because the tool list is the start of every request and has to
  stay the same for the prompt cache.
- **When a tool is called**, the connection is read again. A call of a tool that has been
  closed to the gem since (or whose connection is off or gone) is answered with "This
  tool is not available to you now.", and a changed address or header is the one used.
  So closing a tool to a gem holds from that moment, even in a call that is already
  open.

**A tool that has gone stays declared.** A conversation may hold calls of a tool the gem
is no longer offered: its connection was deleted, turned off or closed to the gem. A
provider refuses a history with calls of a tool it is not told of, so such a tool is
still declared, with the description "This tool is not available to you now." and an
empty schema, and a call of it is answered with the same words.

## The copy of a server's tools

**A gem is offered the copy, never the server's list of the moment.** The copy is made
when the connection is added, again when its address or headers change, and when the
user asks (`POST /v1/connections/{id}/refresh`). So the descriptions a model reads are
the ones the user was shown: a server cannot reword a tool, or add one, behind the
user's back. Opening a reply costs no request to the server, and the tool list does not
move between requests.

- **If the server does not answer** (a 502), the copy stays as it was. So does a
  refresh of a server that now asks for a sign-in: the connection then needs one.
- **Only the fields sent are written** by a change, so a change made while a server is
  being asked is not undone by it.

What it does not cover: a new tool that a refresh brings in is open to whoever the
connection is open to, so look at what a refresh lists. A limit is kept for a tool the
server stops listing, so one that comes back comes back as closed as it was left.

**What is left out of the copy**, and counted in `tools_left_out`: a tool with no name or
a name listed twice (or one the model would be told by the same name as another), one
whose schema is not an object's, is not a valid JSON schema, has `anyOf`, `oneOf`,
`allOf`, `enum` or `not` at its top, names an argument with letters a provider refuses,
or is longer than 20,000 characters, and everything after the first 128. One tool a
provider refuses fails the whole request, so a doubtful one is left out rather than
tried.

**Across all its connections a gem is offered at most 100 tools.** The ones after that
are passed over, and a line is logged. Every tool a gem is offered is sent with every
request (unless its connection is deferred), so a connection with many tools costs
context and, in a call, time before the first word. Open a connection to the gems that
need it, and turn off the tools they do not.

## The server's icons

An MCP server may state its own icons and its website when a client opens (its
`serverInfo`). They are read with the tools, from the same open client, and kept with
the copy: written and replaced with it, and dropped with it when a new address needs a
sign-in. Only the backend can read them, as most servers refuse a client that has not
signed in, so the app gets them from the connection.

- **An icon is kept as a `data:` address**, so the app never reaches a host for one. One
  the server gives as `data:` is kept as it is. One it names by an `https:` address is
  fetched once, here, with none of the connection's headers and no pass, and no redirect
  followed. A plain `http:` one is not fetched.
- **What is kept**: PNG, JPEG, WebP or SVG, judged by what came back (the `data:`
  address's own type, or the answer's `Content-Type`), not by what the server said it
  would be. At most 64 KB each, and no more than that is read. The first three that can
  be kept, of the first eight the server states, in its order.
- **It never fails a connection.** An icon that cannot be had, or a server that states
  none, leaves `icons` empty. All of them are fetched at once, within 5 seconds.
- **What it does not cover**: a server can show any picture, another company's logo too.
  The connection's name and address are what say which server it is. An SVG is passed on
  as the server gave it, for the app to draw as a picture, never as a page.

## What a tool call does

- **Arguments.** The model's arguments go to the server as they are. The tool's schema is
  the server's, so it is sent to the model with `strict` off, and the arguments are the
  model's best effort.
- **The result comes back as text**: text parts as they are, a link as its name and
  address, a picture or a sound as a line saying it is not shown. A result longer than
  20,000 characters is cut there, with a line saying how long it was: a result is kept
  whole in the conversation and sent with every request after it.
- **Failures.** A tool that fails comes back as "The tool failed: ...". A server that
  cannot be reached, answers 401, or takes more than 60 seconds comes back as "`{tool}`
  failed: ...", so the gem can say so.
- One client is kept open for each server and shared by every call to it.

## Deferred tools

A connection with `deferred` on keeps its tools out of the start of every request. A gem
offered at least one deferred tool is also offered one tool of ours, `tool_search`, never
deferred. Its description says that more tools are behind it and lists each deferred
connection by name with how many tools it has.

Cortex runs the search itself: it scores each deferred tool by how many of the query's
words are in its name, description and argument names, and returns the best eight, by
name. Each provider's own deferred loading then loads them, and a loaded tool's
definition lands at the end of the context, where the search was:

- **Anthropic.** Every deferred tool stays in `tools`, with `defer_loading: true`, on
  every request; the model is not shown one until it is loaded. The search's result is
  sent as a `tool_result` of `tool_reference` blocks, one per tool found. No cache mark
  is ever put on a deferred tool (the API refuses that).
- **OpenAI.** The request's tools are the ones not deferred plus `tool_search` in client
  mode. The search comes back as a `tool_search_call`, and is answered with a
  `tool_search_output` that holds the full definitions of the tools found. Deferred tools
  are not in the request's tools at all.

The search is an ordinary call and result in the one history, known by its name,
`tool_search` (no connection's tool can have it: theirs have `__`). Only the translation
for OpenAI turns it into that provider's search items. So the history works on either
provider, and a loaded tool stays loaded for the rest of the conversation.

- **The cache holds.** The request's tools and instructions are identical from first to
  last. On both providers each request after a search read the whole of the request
  before it from the cache (`scripts/deferred_tools_check.py`).
- **The cost** is one extra model round before a tool's first use. In a call that is a
  wait before the first word.
- **Turning `deferred` on or off** applies from the next reply or call, like any change
  to a connection. A conversation that called a connection's tools before it was
  deferred is still accepted by both providers, with no search before those calls.
- A tool a search loaded and that is no longer offered stays declared, deferred, as not
  available: a reference to a tool that is not declared is refused.

## Signing in through the browser

A server can let us in by a fixed header, or by a sign-in in the user's browser (the
[MCP authorization spec](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization),
OAuth). Add a connection with only its address: if its server answers 401 and publishes
how to sign in, the connection is kept (201), with no tools, needing a sign-in. A
sign-in is tried only for a connection with no `Authorization` header of its own; one
whose header is refused, or whose server publishes nothing, is a 502.

1. **`POST /v1/connections/{id}/sign-in`** asks the server again with no pass, follows
   its 401 to its protected resource metadata and from there to its sign-in server,
   registers this backend there as a client (dynamic client registration) unless it
   already is, and answers with the address to open in the browser. The sign-in is an
   authorization code with PKCE (S256). What is needed to finish it is held in memory
   under a random `state`, for 10 minutes, and used once: a restart loses it, and the
   user begins again.
2. **The browser comes back to `GET /v1/connections/sign-in/callback`, on this backend**,
   at the address set by `BACKEND_URL` (default `http://127.0.0.1:8000`). That works
   because the backend runs on the user's own Mac, where the browser is; it has to be
   rethought when the backend is deployed or runs on another machine. The code is
   swapped for a pass, `iss` is checked when the sign-in server says it sends it, the
   pass is kept, and the server is asked for its tools as a refresh does. The browser is
   shown a short page: signed in, go back to Cortex, or what went wrong. Every socket of
   the app is told `connection.changed`.

A registration made with another `BACKEND_URL`, or with another sign-in server, is not
used: the next sign-in registers again. A refresh token is asked for wherever the
metadata allows (the `refresh_token` grant, and `offline_access` when it is one of the
scopes).

**A pass that runs out is renewed without the user** when there is a refresh token: in
the minute before it runs out, or when the server answers 401, and the request is made
once more. One renewal at a time for a connection.

**A pass that cannot be renewed** (no refresh token, or the sign-in server refuses it) is
dropped the moment the server refuses it, or when it is used past its time. The
connection then needs a sign-in, every socket is told `connection.changed`, and the
gem's call is answered "This tool needs the user to sign in to {name} again." within
seconds. A tool call never waits on a browser. A connection that needs a sign-in still
offers the tools it has, so a call's tool list does not change, and the gem can tell the
user why a call did not run.

A 403 is not a sign-in: it fails the call, and changes nothing. A sign-in server that
cannot be reached leaves the pass as it is.

**What a client is told** is `sign_in`, its state, when the pass runs out and whether it
renews (see [the contract](chat-contract.md#signing-in-through-the-browser)). A pass
past its time that cannot be renewed reads as `"sign_in_needed"`, worked out when it is
read.

**Secrets stay here.** The pass, the refresh token, the registration and the PKCE secret
never go to a client, a frame, the model or a log. They are kept in the database as
given, like the headers. Two connections to the same address each sign in on their own.
A new `url` drops the sign-in (the pass and the registration) and the copy of the tools,
and signs in again if the new server asks; giving an `Authorization` header of the
user's own drops it too. Deleting a connection drops everything.

## Not built, on purpose

- **Servers started as a local command (stdio), and the older SSE transport.** These
  routes have no sign-in, and the API is open to any web page while `DEBUG` is on. A
  route that starts a command would let whoever can reach it run one on this machine.
  It can come with the sign-in.
- **Sign-ins this backend cannot do.** A sign-in server that does not let a client
  register itself (no registration endpoint), one that takes no code with PKCE (S256),
  one whose metadata names another server, client ID metadata documents, and scopes
  asked for later (a 403 step-up).
- **Being told before a pass runs out.** Nothing runs on a timer. The app shows a badge
  from `sign_in`; there is no notification.
- **A vault.** The headers, passes and registrations are kept in the database as they
  are given. They are never sent to a client or to the model.
