# Watches

A gem can watch things: something that happens wakes it, and it does what its
instructions say. The gem makes a watch itself, with a tool call, and every watch
belongs to that one gem: none is free. The app shows each gem's watches, and the user
can remove one.

This page says how the backend keeps and runs watches. The routes, the shape the app
reads and the `watch.changed` frame are in [the contract](chat-contract.md#watches). The
code is [`app/entities/watch_ent.py`](../app/entities/watch_ent.py), and the shapes that
cross between its parts are in [`app/entities/gem_event.py`](../app/entities/gem_event.py).

## Three sources

| Source | What wakes the gem | How it runs |
| --- | --- | --- |
| `mcp` | An event a connection's server sends (MCP events) | In a task of its own, through the connections (`McpEvents.watch`), for as long as the watch does |
| `webhook` | A post to an address of ours, such as a GitHub repo's own webhook | Nothing runs: a post comes to `POST /v1/hooks/{token}` |
| `timer` | Every so many minutes | In a task of its own, asleep until it is due |

Whatever its source, an event is handed to the event sink as a `GemEvent`: which gem and
watch, what happened (its name and its data as text, cut to 20,000 characters), when,
and the source's own id for it, if it has one. The sink makes it part of the gem's
conversation. An event is counted, and an MCP watch's cursor moved, only once the sink
has kept it.

Every watch that is not paused runs from when the backend starts until it stops.

## MCP events

A gem may watch only the events of a connection open to it: `watchable` lists them,
with the arguments each takes, and `watch` refuses any other. Which delivery is used
(stream, webhook or poll) is the connections' choice, never the gem's.

The cursor after each event is kept, so after a restart the watch goes on from where it
left off. Each MCP watch has a webhook address too, for a server that delivers by
posting to one: a post to it is checked and read by the connections, not here.

## Webhooks

A webhook watch's address is `{PUBLIC_URL}/v1/hooks/{token}`. The token is 32 random
bytes, in URL-safe base64: it cannot be guessed, and it is the secret too.

- **The gem registers it itself.** The `watch` tool's result gives the gem the address,
  the secret (the same token) and the content type, `application/json`, so it can add the
  webhook where the events come from, such as with GitHub's tools. So the secret is in
  the gem's conversation, and in its log, and is sent to its model. The app's routes
  never send it.
- **A signed post must be signed with it.** A post with `X-Hub-Signature-256` (GitHub's
  way: `sha256=` and the HMAC-SHA256 of the body) is refused unless it matches, compared
  in constant time. A post with no signature is taken: the address is the secret.
- **What the gem is told.** The event is named by `X-GitHub-Event`, or `webhook`; its id
  is `X-GitHub-Delivery`; its data is the body as UTF-8 text. A GitHub `ping` wakes
  nobody.
- **What is refused.** A body over 1 MB, unread past that. What was posted is never sent
  back, and never logged.

With no `PUBLIC_URL`, the gem is told that no webhook can be made.

### Exposing only the hooks

The backend runs on the user's Mac. Webhooks need it reachable from outside, which a
tunnel gives, and the tunnel must let through `/v1/hooks/` and nothing else: every
other route has no sign-in. With cloudflared, for example (the hostname is a
placeholder):

```yaml
ingress:
  - hostname: hooks.example.com
    path: ^/v1/hooks/
    service: http://127.0.0.1:8000
  - service: http_status:404
```

Then set `PUBLIC_URL=https://hooks.example.com`.

The access log of the server prints each request's path. A webhook's token is left out
of it: the path is shown as `/v1/hooks/…`.

## Timers

A timer goes off every `every_minutes`, at least every 5 minutes (**not yet ruled
on**). It is first due one interval after it is made. When it goes off it is next due
one interval after it was due, so it keeps its rhythm. One that came due while the
backend was down, once or many times, goes off once as soon as the backend is up, and
then keeps its rhythm. Its event is named `timer`, and has no data.

## When a watch fails

A watch is `active`, `failing` or `paused`, and when it is not active its `reason` says
why.

- **Failing.** An MCP watch whose connection cannot run it (the connection is gone, is
  no longer open to the gem, or does not answer), or a timer whose event the sink cannot
  keep, is failing. It is tried again after 10 seconds, then twice as long each time,
  at most every ten minutes. An MCP watch is active again once a try has run for a
  minute or brought an event; a timer once its event is kept. The 10 seconds and the
  minute are **not yet ruled on**.
- **A post that cannot be kept** is answered 503, and nothing is counted, so the sender
  may try again.

## The fuse

A gem can wake itself in a loop: it comments on a pull request, the webhook fires, it
wakes and comments again. So a watch that brings more than 60 events in one hour is
paused, and brings no more until it is removed and made again (**not yet ruled on**).
The count is kept in memory, so a restart starts it again. A post to a paused watch is
answered 409.

## What ends a watch

- **The gem** removes one with `unwatch`, and **the user** in the app. Either way it stops
  at once, and the gem's log gets `watch.removed`, saying by whom.
- **A reset** of the gem removes all its watches (`end_for`), each written down as
  removed by the user.
- **Deleting a gem** removes its watches with it, and writes nothing: its log is gone.

## The gem's tools

| Tool | Does |
| --- | --- |
| `watch` | Makes a watch: `about` (what it is for, told with every event) and `source` (`event`, `webhook` or `timer`), with `connection`, `event` and `arguments` for an event, `every_minutes` for a timer |
| `unwatch` | Removes one of the gem's own watches, by `id` |
| `watches` | Lists the gem's watches, with their state |
| `watchable` | Lists the MCP events the gem may watch, with the arguments each takes |

They are the same for every gem, every time, so the start of a request does not move.
`watch` is not strict, since what it needs differs by source and an event's arguments
are the event's own; the others take an id or nothing. A refusal is told to the gem in
words it can act on.
