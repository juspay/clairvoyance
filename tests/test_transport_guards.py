"""Guards on the template HTTP transport that a review found missing.

1. SSRF: the URL validator has NO environment-dependent carve-out. Plain
   http is refused everywhere, and a loopback target stays blocked even
   over https — in dev exactly as in production. (An earlier draft of this
   branch admitted plain-http loopback outside production; review removed
   it, and this test pins the unconditional posture.)
2. ``retry_until`` re-issues a request verbatim, so ``HttpRequestConfig``
   refuses it on any mutating method at load time.
3. ``TOOL_RESULT`` pointers decode RFC 6901 escapes (``~1`` → ``/``,
   ``~0`` → ``~``) before each lookup.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

# isort: off
# template.types must load before the transport modules (see the note in
# test_response_transform.py — reversing the order trips a circular import).
from app.ai.voice.agents.breeze_buddy.template.types import (
    FieldConfig,
    FieldSource,
    HttpRequestConfig,
    RetryUntilConfig,
)

import app.ai.voice.agents.breeze_buddy.handlers.transport.http_requester as hr
from app.ai.voice.agents.breeze_buddy.handlers.transport.utils.field_resolver import (
    FieldResolver,
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
# TOOL_RESULT pointer decoding
# ---------------------------------------------------------------------------


class _Store:
    def __init__(self, payload):
        self._payload = payload

    def resolve(self, tool_name, tool_use_id=None):
        return self._payload if tool_name == "t" else None


class _Bot:
    def __init__(self, payload):
        self.binding_store = _Store(payload)


class _Ctx:
    def __init__(self, payload):
        self.bot = _Bot(payload)


def _resolve(payload, expr):
    resolver = FieldResolver(
        context=_Ctx(payload),  # pyrefly: ignore[bad-argument-type]
        args={},
    )

    return resolver._resolve_tool_result(
        FieldConfig(source=FieldSource.TOOL_RESULT, value=expr), "f"
    )


def test_tool_result_pointer_decodes_rfc6901_escapes():
    payload = {"a/b": {"m~n": ["x", "y"]}, "plain": 1}
    assert _resolve(payload, "t#/a~1b/m~0n/1") == "y"
    assert _resolve(payload, "t#/plain") == 1
    # the undecoded literal is not a key
    assert _resolve({"a~1b": 1}, "t#/a~1b") is None
