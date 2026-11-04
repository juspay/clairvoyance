"""
Some callers can send a credential only in the URL: DevCycle's webhook
(``?secret=``), the UAP webhook (``?token=``), Exotel's answer URL
(``?auth_token=``), Meta's webhook handshake (``?hub.verify_token=``) and
the console's STT WebSocket (``?token=``, the user's login JWT). uvicorn logs the full URL of every request (``uvicorn.access``)
and WebSocket handshake (``uvicorn.error``), and run.py routes both through
InterceptHandler, so neither line may carry those values.
"""

import logging
from typing import Iterator, List

import pytest

from app.core.logger import InterceptHandler, logger

SECRET = "s3cr3t-value"
CLIENT = "203.0.113.7:5000"


def access_record(path: str) -> logging.LogRecord:
    """A record shaped like uvicorn's access log: client, method, path, version, status."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=(CLIENT, "POST", path, "1.1", 200),
        exc_info=None,
    )


def websocket_record(path: str, outcome: str) -> logging.LogRecord:
    """A record shaped like uvicorn's WebSocket handshake line, which uvicorn
    logs on uvicorn.error: client, path with query, then the outcome."""
    return logging.LogRecord(
        name="uvicorn.error",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=f'%s - "WebSocket %s" {outcome}',
        args=(CLIENT, path),
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
        # DevCycle's webhook secret
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
        # the UAP webhook's shared token
        (
            f"/agent/voice/breeze-buddy/uap/webhook?token={SECRET}",
            "/agent/voice/breeze-buddy/uap/webhook?token=[redacted]",
        ),
        # Meta's webhook handshake, sent when the callback URL is saved
        (
            f"/crm/ingest/webhooks/meta?hub.mode=subscribe&hub.challenge=7&hub.verify_token={SECRET}",
            "/crm/ingest/webhooks/meta?hub.mode=subscribe&hub.challenge=7&hub.verify_token=[redacted]",
        ),
        # Exotel's answer URL
        (
            f"/agent/voice/breeze-buddy/exotel/answer?CallSid=CA1&auth_token={SECRET}",
            "/agent/voice/breeze-buddy/exotel/answer?CallSid=CA1&auth_token=[redacted]",
        ),
    ],
)
def test_the_access_log_never_shows_a_url_credential(
    logged: List[str], path: str, shown: str
) -> None:
    InterceptHandler().emit(access_record(path))

    assert logged == [f'{CLIENT} - "POST {shown} HTTP/1.1" 200']
    assert SECRET not in logged[0]


@pytest.mark.parametrize("outcome", ["[accepted]", "403"])
def test_a_websocket_handshake_line_never_shows_the_login_token(
    logged: List[str], outcome: str
) -> None:
    """The STT socket carries the user's RBAC JWT as ?token=; uvicorn logs the
    handshake, accepted or refused, on uvicorn.error."""
    path = f"/agent/voice/breeze-buddy/stt/ws?language=en&token={SECRET}"

    InterceptHandler().emit(websocket_record(path, outcome))

    shown = "/agent/voice/breeze-buddy/stt/ws?language=en&token=[redacted]"
    assert logged == [f'{CLIENT} - "WebSocket {shown}" {outcome}']
    assert SECRET not in logged[0]


def test_other_query_parameters_are_logged_as_they_are(logged: List[str]) -> None:
    path = "/agent/voice/breeze-buddy/leads?not_secret=keep&page=2&tokens=3"

    InterceptHandler().emit(access_record(path))

    assert logged == [f'{CLIENT} - "POST {path} HTTP/1.1" 200']


def test_other_loggers_are_left_alone(logged: List[str]) -> None:
    """Only uvicorn's URL lines are rewritten; an app log is passed as is."""
    record = access_record(f"/x?token={SECRET}")
    record.name = "app.some_module"

    InterceptHandler().emit(record)

    assert logged == [f'{CLIENT} - "POST /x?token={SECRET} HTTP/1.1" 200']
