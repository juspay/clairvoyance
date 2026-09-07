"""Guards on the template HTTP transport that a review found missing.

1. SSRF: the URL validator has NO environment-dependent carve-out. Plain
   http is refused everywhere, and a loopback target stays blocked even
   over https — in dev exactly as in production. (An earlier draft of this
   branch admitted plain-http loopback outside production; review removed
   it, and this test pins the unconditional posture.)
2. ``retry_until`` re-issues a request verbatim, so ``HttpRequestConfig``
   refuses it on any mutating method at load time; polls run with no
   transport retries and a failed poll keeps the last good body.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

# isort: off
# template.types must load before the transport modules (see the note in
# test_response_transform.py — reversing the order trips a circular import).
from app.ai.voice.agents.breeze_buddy.template.types import (
    HttpRequestConfig,
    RetryUntilConfig,
)

import app.ai.voice.agents.breeze_buddy.handlers.transport.http_requester as hr
from app.ai.voice.agents.breeze_buddy.handlers.transport.http_handler import (
    _poll_until_ready,
)

# isort: on

_validate = hr.HttpRequestExecutor._validate_resolved_url


# ---------------------------------------------------------------------------
# SSRF: no environment carve-out
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("env", ["dev", "development", "staging", "production"])
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8787/pay",
        "http://localhost/pay",
        "http://[::1]:8787/pay",
        "http://example.com/x",
    ],
)
def test_plain_http_is_refused_in_every_environment(monkeypatch, env, url):
    monkeypatch.setattr(hr, "ENVIRONMENT", env)
    with pytest.raises(ValueError, match="Only HTTPS"):
        _validate(url)


@pytest.mark.parametrize("env", ["dev", "production"])
def test_loopback_and_private_targets_blocked_over_https_too(monkeypatch, env):
    monkeypatch.setattr(hr, "ENVIRONMENT", env)
    # production refuses by hostname first ("localhost ... not allowed"),
    # dev by address class ("loopback") — refused either way.
    with pytest.raises(ValueError, match="not allowed"):
        _validate("https://127.0.0.1/x")
    with pytest.raises(ValueError, match="not allowed"):
        _validate("https://[::1]/x")

    with pytest.raises(ValueError, match="private"):
        _validate("https://192.168.1.1/x")
    with pytest.raises(ValueError):
        _validate("https://169.254.169.254/latest/meta-data")
    _validate("https://api.example.com/v1")  # public https is always fine


# ---------------------------------------------------------------------------
# retry_until is GET-only
# ---------------------------------------------------------------------------


def test_retry_until_allowed_on_get():
    cfg = HttpRequestConfig(
        url="https://api.example.com/journeys/{id}",
        method="GET",
        retry_until=RetryUntilConfig(field="allJourneysLoaded"),
    )
    assert cfg.retry_until is not None


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_retry_until_refused_on_mutating_methods(method):
    with pytest.raises(
        ValidationError, match="retry_until is only allowed with method GET"
    ):
        HttpRequestConfig(
            url="https://api.example.com/orders",
            method=method,
            retry_until=RetryUntilConfig(field="ready"),
        )


def test_retry_until_default_method_is_post_so_it_must_be_explicit():
    with pytest.raises(ValidationError):
        HttpRequestConfig(
            url="https://api.example.com/orders",
            retry_until=RetryUntilConfig(field="ready"),
        )


# ---------------------------------------------------------------------------
# retry_until: bounded polling
# ---------------------------------------------------------------------------


class _Executor:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list = []

    async def execute(self, *, config, resolved_fields, fire_and_forget, on_sse_event):
        self.calls.append(config)
        return self.responses.pop(0)


class _Forwarder:
    def __init__(self, chunks=None):
        self.chunks = chunks or []


NOT_READY = (200, '{"allJourneysLoaded": false}')
READY = (200, '{"allJourneysLoaded": true}')


def _cfg(**kw) -> RetryUntilConfig:
    base = dict(field="allJourneysLoaded", delay_ms=100, max_attempts=3)
    base.update(kw)
    return RetryUntilConfig(**base)


async def _poll(executor, retry_until, first, chunks=None):
    request = HttpRequestConfig(
        url="https://api.example.com/journeys/1",
        method="GET",
        timeout=10,
        max_retries=3,
        retry_until=retry_until,
    )
    return await _poll_until_ready(
        executor,  # pyrefly: ignore[bad-argument-type]
        request,
        {},
        _Forwarder(chunks),
        first,
        "get_journeys",
    )


async def test_poll_stops_when_ready_and_never_inherits_transport_retries():
    ex = _Executor([READY])
    assert await _poll(ex, _cfg(), NOT_READY) == READY
    assert len(ex.calls) == 1
    poll_request = ex.calls[0]
    assert poll_request.max_retries == 1 and poll_request.timeout == 10
    assert (
        poll_request.url.endswith("/journeys/1") and poll_request.method.value == "GET"
    )


async def test_failed_poll_keeps_last_good_body_and_stops():
    ex = _Executor([(0, ""), READY])
    assert await _poll(ex, _cfg(), NOT_READY) == NOT_READY  # never a failed poll
    assert len(ex.calls) == 1  # a failed poll ends the loop
    ex = _Executor([(503, "upstream")])
    assert await _poll(ex, _cfg(), NOT_READY) == NOT_READY


async def test_no_poll_when_first_response_is_ready_failed_or_sse():
    ex = _Executor([])
    assert await _poll(ex, _cfg(), READY) == READY
    assert await _poll(ex, _cfg(), (500, "boom")) == (500, "boom")
    assert await _poll(ex, _cfg(), (0, "")) == (0, "")
    assert await _poll(ex, _cfg(), NOT_READY, chunks=["event"]) == NOT_READY
    assert ex.calls == []


def test_retry_until_equals_is_a_json_scalar():
    for ok in (True, 0, 1.5, "done", None):
        assert RetryUntilConfig(field="f", equals=ok).equals == ok
    with pytest.raises(ValidationError):
        RetryUntilConfig.model_validate({"field": "f", "equals": ["ready"]})
    with pytest.raises(ValidationError):
        RetryUntilConfig.model_validate({"field": "f", "equals": {"ready": True}})
