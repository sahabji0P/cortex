"""Run the test MCP server that serves events, on a port of this machine, for a live trial.

Run from the repository root:

    uv run python -m scripts.mcp_events_server              # http://127.0.0.1:8765/mcp
    uv run python -m scripts.mcp_events_server 9000         # another port
    uv run python -m scripts.mcp_events_server 9000 TOKEN   # webhooks need this pass

Add it to Cortex as a connection with that address (and, with a token, the header
`Authorization: Bearer TOKEN`). Its two kinds of event are `issue.opened` (poll, push
and webhook; the argument `repo`) and `build.finished` (poll only). Send one with:

    curl -s localhost:8765/emit -d '{"name": "issue.opened", "data": {"repo": "a/b", "number": 7}}'

The server is the one the tests use (`tests/mcp_events_server.py`). It takes a webhook
address over plain http, which a real server must not, so a backend on this machine can
be posted to. Everything it keeps is lost when it stops.
"""

import asyncio
import sys

from tests.mcp_events_server import EventsServer, running

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
TOKEN = sys.argv[2] if len(sys.argv) > 2 else None


async def main() -> None:
    server = EventsServer(allow_http=True, token=TOKEN)
    async with running(server, PORT) as served:
        print(f"Serving events at {served.url}. Send one with POST /emit. Ctrl-C stops it.")
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
