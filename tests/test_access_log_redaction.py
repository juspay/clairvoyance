"""
DevCycle can authenticate its webhook only with ``?secret=`` in the URL (it
signs nothing and sends no custom headers), so uvicorn's access-log line,
which run.py routes through InterceptHandler, must not carry that value.
"""

import logging
from typing import Iterator, List

import pytest

from app.core.logger import InterceptHandler, logger

SECRET = "s3cr3t-devcycle-value"


def access_record(path: str) -> logging.LogRecord:
    """A record shaped like uvicorn's access log: client, method, path, version, status."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("203.0.113.7:5000", "POST", path, "1.1", 200),
        exc_info=None,
    )


@pytest.fixture
def logged() -> Iterator[List[str]]:
    lines: List[str] = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="DEBUG")
    yield lines
    logger.remove(sink)


@pytest.mark.parametrize(
    "path, shown",
    [
        (f"/webhooks/devcycle?secret={SECRET}", "/webhooks/devcycle?secret=[redacted]"),
        # the trailing-slash redirect is logged too, secret and all
        (
            f"/webhooks/devcycle/?secret={SECRET}",
            "/webhooks/devcycle/?secret=[redacted]",
        ),
        (
            f"/webhooks/devcycle?env=prod&secret={SECRET}&v=2",
            "/webhooks/devcycle?env=prod&secret=[redacted]&v=2",
        ),
        (f"/webhooks/devcycle?SECRET={SECRET}", "/webhooks/devcycle?SECRET=[redacted]"),
    ],
)
def test_the_access_log_never_shows_the_secret(
    logged: List[str], path: str, shown: str
) -> None:
    InterceptHandler().emit(access_record(path))

    assert logged == [f'203.0.113.7:5000 - "POST {shown} HTTP/1.1" 200']
    assert SECRET not in logged[0]


def test_other_query_parameters_are_logged_as_they_are(logged: List[str]) -> None:
    path = "/agent/voice/breeze-buddy/leads?not_secret=keep&page=2"

    InterceptHandler().emit(access_record(path))

    assert logged == [f'203.0.113.7:5000 - "POST {path} HTTP/1.1" 200']
