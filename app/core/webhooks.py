"""
Webhook signatures, as the open Standard Webhooks scheme makes them.

A post signed that way carries three headers: `webhook-id`, its own id, the same each
time it is sent again; `webhook-timestamp`, when it was sent, in whole seconds since the
epoch; and `webhook-signature`, one or more signatures, each `v1,` and the HMAC-SHA256 of
`{id}.{timestamp}.{body}` in base64, with a space between two. A post is believed if any
`v1` signature holds and it was sent within five minutes of now.

Which key a signature is made with is the caller's: a connection's server signs with
the bytes of its `whsec_` secret, a plain webhook with its secret as it is.
"""

import base64
import hashlib
import hmac
from collections.abc import Mapping
from datetime import UTC, datetime

__all__ = ["STANDARD_WEBHOOK_FRESH_SECONDS", "standard_webhook_id"]

# How far from now a post's time may be, either way.
STANDARD_WEBHOOK_FRESH_SECONDS = 300


def standard_webhook_id(
    key: bytes, headers: Mapping[str, str], body: bytes, now: datetime
) -> str | None:
    """The post's `webhook-id`, if it is signed with `key` as Standard Webhooks signs one.

    None if any of the three headers is missing, its time is not a number or is more than
    five minutes from `now`, or no `v1` signature it carries holds. The body is checked as
    it came, and the signatures are compared in constant time.
    """
    lower = {name.lower(): value for name, value in headers.items()}
    message_id = lower.get("webhook-id")
    stamp = lower.get("webhook-timestamp")
    signatures = lower.get("webhook-signature")
    if not message_id or not stamp or not signatures:
        return None
    try:
        sent = datetime.fromtimestamp(int(stamp), UTC)
    except (ValueError, OverflowError, OSError):
        return None
    if abs((now - sent).total_seconds()) > STANDARD_WEBHOOK_FRESH_SECONDS:
        return None
    signed = f"{message_id}.{stamp}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    held = any(
        version == "v1" and hmac.compare_digest(given.encode(), expected.encode())
        for version, _, given in (one.partition(",") for one in signatures.split())
    )
    return message_id if held else None
