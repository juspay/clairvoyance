"""End-to-end proof that telephony webhook verification holds on the real app.

These drive the actual mounted routes over real HTTP sockets, with signatures
produced by the providers' own SDKs. Unit tests build Starlette requests in
process; only this suite proves the wiring — router mounting, form parsing,
canonical URL reconstruction and the 401 short-circuit — behaves as intended
when a real client talks to a real server.
"""

from typing import Dict

import pytest

from tests.e2e_security.conftest import EXOTEL_TOKEN, PLIVO_TOKEN, TWILIO_TOKEN

STATUS = "/agent/voice/breeze-buddy/{p}/callback/status"
ANSWER = "/agent/voice/breeze-buddy/{p}/answer"
FALLBACK = "/agent/voice/breeze-buddy/twilio/callback/twiml-fallback"

TWILIO_FORM = {"CallSid": "CA-e2e-synthetic", "CallStatus": "ringing"}
PLIVO_FORM = {"CallUUID": "PL-e2e-synthetic", "CallStatus": "ringing"}


def twilio_sig(url: str, params: Dict[str, str], token: str = TWILIO_TOKEN) -> str:
    from twilio.request_validator import RequestValidator

    return RequestValidator(token).compute_signature(url, params)


def plivo_v3(
    url: str, params: Dict[str, str], nonce: str, token: str = PLIVO_TOKEN
) -> str:
    from plivo.utils.signature_v3 import construct_post_url, get_signature_v3

    base = construct_post_url(url, params).decode("utf-8")
    return get_signature_v3(token.encode("utf-8"), base, nonce.encode("utf-8")).decode(
        "utf-8"
    )


# --------------------------------------------------------------------------
# Exotel — shared-secret token on the query string
# --------------------------------------------------------------------------


def test_exotel_status_without_token_is_rejected(client):
    r = client.post(
        STATUS.format(p="exotel"), data={"CallSid": "EX1", "Status": "in-progress"}
    )
    assert r.status_code == 401


def test_exotel_status_with_wrong_token_is_rejected(client):
    r = client.post(
        STATUS.format(p="exotel") + "?auth_token=wrong-token",
        data={"CallSid": "EX1", "Status": "in-progress"},
    )
    assert r.status_code == 401


def test_exotel_status_with_correct_token_passes_the_verifier(client):
    r = client.post(
        STATUS.format(p="exotel") + f"?auth_token={EXOTEL_TOKEN}",
        data={"CallSid": "EX1", "Status": "in-progress"},
    )
    assert r.status_code != 401


# --------------------------------------------------------------------------
# Twilio — HMAC signature
# --------------------------------------------------------------------------


def test_twilio_status_without_signature_is_rejected(client):
    assert client.post(STATUS.format(p="twilio"), data=TWILIO_FORM).status_code == 401


def test_twilio_status_with_forged_signature_is_rejected(client):
    r = client.post(
        STATUS.format(p="twilio"),
        data=TWILIO_FORM,
        headers={"X-Twilio-Signature": "AAAAforgedAAAA="},
    )
    assert r.status_code == 401


def test_twilio_status_with_sdk_generated_signature_passes(client, launched_app):
    url = f"{launched_app.base}{STATUS.format(p='twilio')}"
    r = client.post(
        STATUS.format(p="twilio"),
        data=TWILIO_FORM,
        headers={"X-Twilio-Signature": twilio_sig(url, TWILIO_FORM)},
    )
    assert r.status_code != 401


def test_twilio_signature_binds_the_form_fields(client, launched_app):
    """Sign one payload, send another: the signature must not still validate."""
    url = f"{launched_app.base}{STATUS.format(p='twilio')}"
    sig = twilio_sig(url, TWILIO_FORM)
    tampered = dict(TWILIO_FORM, CallStatus="completed")
    r = client.post(
        STATUS.format(p="twilio"), data=tampered, headers={"X-Twilio-Signature": sig}
    )
    assert r.status_code == 401


def test_twilio_signature_is_checked_against_app_base_url_not_the_host_header(client):
    """A signature computed for another origin must not authenticate here.

    Sends a hostile ``Host`` header *and* a signature computed for that same
    hostile origin. If the verifier reconstructed the signed URL from the
    request's host, the two would agree and this would authenticate. It must be
    rejected, because the URL is rebuilt from the configured ``APP_BASE_URL``.
    """
    hostile = "attacker.example.com"
    r = client.post(
        STATUS.format(p="twilio"),
        data=TWILIO_FORM,
        headers={
            "Host": hostile,
            "X-Twilio-Signature": twilio_sig(
                f"https://{hostile}{STATUS.format(p='twilio')}", TWILIO_FORM
            ),
        },
    )
    assert r.status_code == 401, (
        "signature validated against the attacker-supplied Host header — "
        "the canonical URL must come from APP_BASE_URL"
    )


# --------------------------------------------------------------------------
# Plivo — V3 signature
# --------------------------------------------------------------------------


def test_plivo_status_without_signature_is_rejected(client):
    assert client.post(STATUS.format(p="plivo"), data=PLIVO_FORM).status_code == 401


def test_plivo_status_with_valid_v3_signature_passes(client, launched_app):
    url = f"{launched_app.base}{STATUS.format(p='plivo')}"
    nonce = "e2e-nonce-12345"
    r = client.post(
        STATUS.format(p="plivo"),
        data=PLIVO_FORM,
        headers={
            "X-Plivo-Signature-V3": plivo_v3(url, PLIVO_FORM, nonce),
            "X-Plivo-Signature-V3-Nonce": nonce,
        },
    )
    assert r.status_code != 401


# --------------------------------------------------------------------------
# Other guarded routes
# --------------------------------------------------------------------------


def test_twiml_fallback_rejects_a_forged_signature(client):
    r = client.post(
        FALLBACK, data=TWILIO_FORM, headers={"X-Twilio-Signature": "forged"}
    )
    assert r.status_code == 401


def test_answer_route_rejects_an_unsupported_provider_before_auth(client):
    assert client.post(ANSWER.format(p="twilio"), data=TWILIO_FORM).status_code == 404


def test_answer_route_rejects_unsigned_plivo(client):
    assert client.post(ANSWER.format(p="plivo"), data=PLIVO_FORM).status_code == 401


def test_answer_route_rejects_wrong_exotel_token(client):
    assert (
        client.post(ANSWER.format(p="exotel") + "?auth_token=nope", data={}).status_code
        == 401
    )


def test_unknown_provider_fails_closed(client):
    assert client.post(STATUS.format(p="vonage"), data={}).status_code == 401


# --------------------------------------------------------------------------
# Log hygiene — the reason this suite exists at all
# --------------------------------------------------------------------------


def _log_after(launched_app, predicate, attempts: int = 25) -> str:
    """Poll the server log until the child process has flushed what we need."""
    import time

    log = ""
    for _ in range(attempts):
        log = launched_app.log
        if predicate(log):
            return log
        time.sleep(0.2)
    return log


def test_hmac_tokens_never_appear_in_logs(client, launched_app):
    # Drive traffic here rather than relying on other tests having run.
    client.post(
        STATUS.format(p="twilio"),
        data=TWILIO_FORM,
        headers={"X-Twilio-Signature": "forged"},
    )
    client.post(STATUS.format(p="plivo"), data=PLIVO_FORM)
    log = launched_app.log
    assert TWILIO_TOKEN not in log
    assert PLIVO_TOKEN not in log


def test_exotel_shared_secret_is_not_written_to_the_access_log(client, launched_app):
    """Regression: Exotel's secret rides in the query string, so Uvicorn's raw
    request line would leak it on every inbound callback. The log patcher
    redacts it; anyone who can read logs must not be able to forge callbacks.

    Self-contained on purpose — it issues the request it asserts about, so it
    cannot pass merely because an earlier test happened to run first.
    """
    client.post(
        STATUS.format(p="exotel") + f"?auth_token={EXOTEL_TOKEN}",
        data={"CallSid": "EX-log-check", "Status": "in-progress"},
    )
    log = _log_after(launched_app, lambda l: "auth_token=" in l)
    assert "auth_token=REDACTED" in log, "access log line was never observed"
    assert EXOTEL_TOKEN not in log, "Exotel shared secret leaked into the logs"


def test_call_details_handler_does_not_log_the_secret(client, launched_app):
    """The call-details handler logs ``dict(request.query_params)`` itself.

    That is an app-level ``logger.*`` call, which does NOT pass through
    ``InterceptHandler`` — so redacting only intercepted records would miss it.
    Redaction lives in the patcher precisely so both routes are covered.
    """
    client.get(
        f"/agent/voice/breeze-buddy/exotel/callback/details?auth_token={EXOTEL_TOKEN}"
        "&CallSid=EX-details-log-check"
    )
    log = _log_after(launched_app, lambda l: "EX-details-log-check" in l)
    assert "EX-details-log-check" in log, "handler log line was never observed"
    assert EXOTEL_TOKEN not in log, "handler leaked the Exotel secret into the logs"


def test_non_ascii_exotel_token_yields_401_not_500(client):
    """A caller-supplied non-ASCII token must not crash the verifier."""
    r = client.post(
        STATUS.format(p="exotel") + "?auth_token=%C3%A9%D1%82%D0%BE",
        data={"CallSid": "EX-non-ascii", "Status": "in-progress"},
    )
    assert r.status_code == 401, f"expected a controlled 401, got {r.status_code}"
