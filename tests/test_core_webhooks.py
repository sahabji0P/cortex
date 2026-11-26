"""The Standard Webhooks check, against the scheme's own published example."""

import base64
from datetime import UTC, datetime, timedelta

from app.core.webhooks import standard_webhook_id

# The example in the Standard Webhooks specification: a `whsec_` secret, whose key is the
# bytes after the prefix, and the signature its libraries make of this post.
_SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
_KEY = base64.b64decode(_SECRET.removeprefix("whsec_"))
_BODY = b'{"test": 2432232314}'
_HEADERS = {
    "webhook-id": "msg_p5jXN8AQM9LWM0D4loKWxJek",
    "webhook-timestamp": "1614265330",
    "webhook-signature": "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE=",
}
_SENT = datetime.fromtimestamp(1614265330, UTC)


def test_the_specification_s_example_holds_with_the_secret_s_bytes() -> None:
    assert standard_webhook_id(_KEY, _HEADERS, _BODY, _SENT) == "msg_p5jXN8AQM9LWM0D4loKWxJek"


def test_keyed_with_the_secret_s_text_it_does_not_hold() -> None:
    assert standard_webhook_id(_SECRET.encode(), _HEADERS, _BODY, _SENT) is None


def test_a_changed_body_or_a_stale_time_does_not_hold() -> None:
    assert standard_webhook_id(_KEY, _HEADERS, _BODY + b" ", _SENT) is None
    assert standard_webhook_id(_KEY, _HEADERS, _BODY, _SENT + timedelta(minutes=6)) is None
