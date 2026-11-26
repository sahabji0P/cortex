"""A webhook's token, the secret that lets a post in, is never printed in the access log."""

import importlib
import logging

# Building the app adds the filter to the access log.
importlib.import_module("app.main")


def _logged(path: str) -> str:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "POST", path, "1.1", 202),
        None,
    )
    for kept in logging.getLogger("uvicorn.access").filters:
        assert not isinstance(kept, logging.Filter) or kept.filter(record)
    return record.getMessage()


def test_a_hook_s_token_is_left_out_of_the_access_log() -> None:
    assert _logged("/v1/hooks/s3cr3t-token") == '127.0.0.1:5000 - "POST /v1/hooks/… HTTP/1.1" 202'


def test_other_paths_are_logged_as_they_are() -> None:
    assert _logged("/v1/gems/abc/watches") == (
        '127.0.0.1:5000 - "POST /v1/gems/abc/watches HTTP/1.1" 202'
    )
