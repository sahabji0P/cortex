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
has kept it. The sink keeps an event with its own id once: delivered again (the same
watch and id, as a sender does when it tries again), it is passed over, and says so. Then
it is not counted in `events`, does not move `last_event_at` and is not told to the fuse,
but it is answered as taken, and a timer's next time or an MCP cursor still moves on.

Every watch that is not paused runs from when the backend starts until it stops.

## MCP events

A gem may watch only the events of a connection open to it: `watchable` lists them,
with the arguments each takes, and `watch` refuses any other. Which delivery is used
(stream, webhook or poll) is the connections' choice, never the gem's.

A watch's `delivery` says how its events reach the backend now, in the words of
a connection's event kinds: `push`, `webhook` or `poll`. The connections tell the watch
the one its server has answered on (a stream opened, a webhook granted, a poll
answered), and it is kept, and told to the app when it changes: so a watch whose server
would not take the webhook says `poll`. It is none until the server first answers. A
plain webhook says `webhook` from when it is made, and a timer none. A watch stored
before watches kept it has none until its server next answers, so a webhook stored then
has none.

The cursor after each event is kept, so after a restart the watch goes on from where it
left off. Each MCP watch has a webhook address too, for a server that delivers by
posting to one: a post to it is checked and read by the connections, not here.

## Webhooks

A webhook watch's address is `{PUBLIC_URL}/v1/hooks/{token}`, and it has a secret its
posts are signed with. Each is 32 random bytes, in URL-safe base64, made apart.

- **Why the token stays in the address.** It is what lets a post in at all: a sender
  that does not sign can post with the address alone. So it must stay unguessable. It is
  not the secret, because an address is seen by more than a secret is: GitHub, for one,
  shows a webhook's address to whoever can see its settings, but never shows its secret
  again once it is saved. A post signed with the secret proves more than one sent to the
  address. A watch made before watches kept a secret of their own has none: its token is
  its secret, so senders set up then keep working.
- **The gem registers it itself.** The `watch` tool's result gives the gem the address,
  the secret and the content type, `application/json`, so it can add the webhook where
  the events come from, such as with GitHub's tools. So the secret is in
  the gem's conversation, and in its log, and is sent to its model. The log routes send
  it too, in that tool's result, as they send everything the gem remembers.
- **The user sees it when they ask.** The app's list of watches and `watch.changed`
  never hold the address or the secret. The app asks for them only when the user clicks
  to see them, with
  [`GET /v1/gems/{gem_id}/watches/{watch_id}/hook`](chat-contract.md#get-v1gemsgem_idwatcheswatch_idhook),
  which only a plain webhook has. With no `PUBLIC_URL` it gives no address, and still
  gives the secret. Like every route but `/v1/hooks/`, it has no sign-in, so it must not
  go through the tunnel.
- **What the gem is told.** The event is named by `X-GitHub-Event`, or `webhook`; its id
  is `X-GitHub-Delivery`, or else `webhook-id`; its data is the body as UTF-8 text. A
  GitHub `ping` wakes nobody.
- **What is refused.** A body over 1 MB, unread past that. What was posted is never sent
  back, and never logged.

With no `PUBLIC_URL`, the gem is told that no webhook can be made.

### Signing

A sender signs a post to prove it knows the secret: it sends, with the post, an HMAC of
the body made with the secret, and the backend makes the same and compares the two, in
constant time. Two ways are known:

| Way | Headers | What is signed |
| --- | --- | --- |
| GitHub's | `X-Hub-Signature-256`: `sha256=` and the HMAC-SHA256, in hex | The body |
| [Standard Webhooks](https://www.standardwebhooks.com/) | `webhook-id`, `webhook-timestamp` (seconds since the epoch) and `webhook-signature`: one or more `v1,` and the HMAC-SHA256 in base64, a space between two | `{webhook-id}.{webhook-timestamp}.{body}` |

- **The key is the secret as it is.** Standard Webhooks' own secrets are `whsec_` and
  base64, and its libraries decode them to make the key. This secret is not one: a
  sender must sign with its text as it is. GitHub does that already.
- **A Standard Webhooks post must be fresh**: its time within five minutes of now, either
  way, so an old post sent again is refused. Its `webhook-id` is its event's id, so one
  sent again within that time is kept once.
- **A post that says it is signed must hold.** A post with either way's signature is
  refused unless it holds, and one with both unless both hold. A Standard Webhooks
  signature without its id or its time is refused.
- **Signed once, signed always.** A post with no signature is taken while the watch has
  never taken a signed one: a sender that never signs keeps working with the address
  alone. Once one signed post is taken (a `ping` is not taken), the watch keeps `signed`,
  and refuses an unsigned post from then on: knowing the address is no longer enough.
  The app shows it as "only signed posts". It is never undone: to take unsigned posts
  again, remove the watch and make it again.

### Refused posts

A post refused for its signature is answered 401, and counted on the watch: `refused`,
and `last_refused_at`. It is never written in the gem's log, and nothing of it is kept.

- **Three in a row make it failing.** After 3 refused posts in a row, with none taken
  between them, the watch is `failing`, with the reason "Posts arrive, but they are not
  signed with this watch's secret": the sender has the address, but the wrong secret, or
  someone else has the address. The gem is told `watch.failing` once (see
  [The gem is told](#the-gem-is-told)), not once a post: anyone with the address could
  otherwise fill its log.
- **A post taken makes it active again**, tells the gem `watch.recovered`, and starts the
  run again. A post delivered again counts as taken here.
- **The run is counted in memory**, as the fuse is, so a restart starts it again. The
  total in `refused` is kept.
- **The app is told of each**: `watch.changed` comes when `refused` moves, so the app can
  show that posts are being refused.
- **Only a plain webhook counts them.** A post to a watch on MCP events is its server's
  to sign, and a refused one is answered 401 and counted nowhere: its connection says
  whether the watch works.

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
  keep, is failing. So is a plain webhook that refused 3 posts in a row (see
  [Refused posts](#refused-posts)), until a post is taken. It is tried again after 10 seconds, then twice as long each time,
  at most every ten minutes. An MCP watch is active again once its server answers (a
  stream opened, a webhook granted, a poll answered) or it brings an event: one that is
  reached but never answers stays failing, and does not flip back and forth. A timer is
  active again once its event is kept. The 10 seconds are **not yet ruled on**.
- **A post that cannot be kept** is answered 503, and nothing is counted, so the sender
  may try again.

### The gem is told

A watch tells its gem when its state changes, once for each change, and when the user
removes it, as an event of its own handed to the sink like any other: written down as `event.came`, told at the gem's
next turn, and waking it if it is quiet.

| Event | When | Its data |
| --- | --- | --- |
| `watch.failing` | An active watch starts failing | The `reason` |
| `watch.paused` | The fuse pauses it | The `reason` |
| `watch.recovered` | A failing watch is active again | None |
| `watch.ended` | The user removes it in the app | "The user removed this watch." |

- **Once.** A failing watch tried again tells nothing more, nor one whose `reason`
  changes while it stays failing: the app is told of that, the gem is not. Each has an
  id of its own change (its name and when it changed), so a later break of the same
  watch is not taken for this one.
- **Counted nowhere.** It is not in the watch's `events`, the fuse does not count it,
  and a paused watch, which brings no more events, still tells it.
- **It never holds the watch up.** It is handed over on its own, after the change is
  kept. If the sink cannot keep it, that is logged, and the gem is not told: a timer
  failing because the sink cannot keep its event is the case, and its gem hears only
  that it works again.
- **Only the user's removal is told.** The app's remove route asks for `watch.ended`
  itself. The gem's own `unwatch` does not (its result already says so), nor a reset
  (the gem starts again knowing nothing, so it would land in a fresh conversation), nor
  deleting the gem. If the sink cannot keep it, that is logged, and the watch is removed
  all the same.

## The fuse

A gem can wake itself in a loop: it comments on a pull request, the webhook fires, it
wakes and comments again. So a watch that brings more than 60 events in one hour is
paused, and brings no more until it is removed and made again (**not yet ruled on**).
Only events kept count: a sender that delivers the same post again and again does not
fill it. It is checked before an event is handed over, so once it holds 60 events kept
within the hour, the next post pauses the watch, even one delivered again. The count is kept in memory, so a restart starts it again. A post to a paused
watch is answered 409.

## What ends a watch

- **The gem** removes one with `unwatch`, and **the user** in the app. Either way it stops
  at once, and the gem's log gets `watch.removed`, saying by whom. When the user removes
  it, the gem is told `watch.ended` (see [The gem is told](#the-gem-is-told)).
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
