"""Lead push — reporting_webhook_url is validated at ingest.

The URL arrives on ``POST /push/lead/v2`` and is not dereferenced until the
call ends, by a background task, minutes or hours later. A bad value used to
be accepted silently there and only fail at delivery time, where the failure
reaches our logs and nobody else — the merchant's push returned 201 long ago.

The check is FORMAT only. Scheme, host, credentials, length. It deliberately
does not resolve the host or judge the address: DNS answers change between the
push and the call, and the egress guard on the sending side owns that question.

http:// stays allowed on purpose. Tenants post to plaintext endpoints today and
rejecting them here would break working integrations; the sender warns instead.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.ai.voice.agents.breeze_buddy.types.models import PushLeadRequest

TEMPLATE_ID = "6b1f0d3c-8a2e-4f5b-9c7d-1e2a3b4c5d6e"


def _req(url: str | None) -> PushLeadRequest:
    return PushLeadRequest(
        request_id="order-1",
        payload={"customer_mobile_number": "+919999999999"},
        template_id=TEMPLATE_ID,
        reseller_id="RESELLER",
        reporting_webhook_url=url,
    )


# --- accepted -------------------------------------------------------------


def test_https_url_is_kept():
    assert _req("https://myshop.com/hooks/calls").reporting_webhook_url == (
        "https://myshop.com/hooks/calls"
    )


def test_http_url_is_still_accepted():
    # Tenants on plaintext endpoints must keep working — the delivery path
    # warns about the scheme, it does not refuse it.
    assert _req("http://myshop.com/hooks/calls").reporting_webhook_url == (
        "http://myshop.com/hooks/calls"
    )


def test_url_with_port_and_query_is_kept():
    url = "https://myshop.com:8443/hooks/calls?token=abc123"
    assert _req(url).reporting_webhook_url == url


def test_surrounding_whitespace_is_stripped():
    assert _req("  https://myshop.com/hook  ").reporting_webhook_url == (
        "https://myshop.com/hook"
    )


@pytest.mark.parametrize("value", [None, "", "   "])
def test_absent_or_blank_means_no_webhook(value):
    # Blank already behaved as "no webhook" downstream (the handler drops it
    # on a falsy check). Turning it into a 422 would break working pushes.
    assert _req(value).reporting_webhook_url is None


# --- rejected -------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "file:///etc/passwd",
        "gopher://myshop.com:6379/_SET%20key",
        "javascript:alert(1)",
        "ftp://myshop.com/hook",
        "myshop.com/hook",  # no scheme at all
    ],
)
def test_non_http_schemes_are_rejected(value):
    with pytest.raises(ValidationError, match="http:// or https://"):
        _req(value)


def test_url_without_a_host_is_rejected():
    with pytest.raises(ValidationError, match="no host"):
        _req("https:///hooks/calls")


def test_embedded_credentials_are_rejected():
    # userinfo is a credential that would be replayed on every outcome and
    # written to every log line that names the destination.
    with pytest.raises(ValidationError, match="must not embed credentials"):
        _req("https://user:s3cret@myshop.com/hook")


def test_invalid_port_is_rejected():
    with pytest.raises(ValidationError, match="not a valid URL"):
        _req("https://myshop.com:notaport/hook")


def test_overlong_url_is_rejected():
    with pytest.raises(ValidationError, match="exceeds 2048 characters"):
        _req("https://myshop.com/" + "a" * 2100)


def test_rejection_never_echoes_the_url():
    # A webhook URL routinely carries a shared secret in the query string.
    # The message must describe the problem without repeating the value.
    secret = "s3cret-token-do-not-log"
    with pytest.raises(ValidationError) as exc:
        _req(f"ftp://myshop.com/hook?token={secret}")
    assert secret not in str(exc.value.errors()[0]["msg"])
