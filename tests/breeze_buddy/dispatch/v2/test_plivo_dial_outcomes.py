"""Plivo dials through the SDK, every request sent exactly once (spec 2026-10-05 §10.3;
SDK facts: docs/superpowers/specs/2026-10-05-plivo-sdk-audit.md §1, §3, §5).
requests.Session.send is faked, so the SDK's own argument checks, body, auth and reply
parsing all run."""

import json
import socket
from typing import Any, List, Tuple, cast

import plivo
import pytest
import requests
import requests.auth
import urllib3
from plivo.exceptions import PlivoRestError
from plivo.version import __version__ as plivo_version
from requests.structures import CaseInsensitiveDict

import app.ai.voice.agents.breeze_buddy.dispatch  # noqa: F401  (import order)
from app.ai.voice.agents.breeze_buddy.services.telephony.base_provider import (
    DIAL_OUTCOME_THROTTLED,
    DIAL_OUTCOME_UNKNOWN,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.plivo import plivo as pv

AUTH_ID, TOKEN = "MAXXXXXXXXXXXXXXXXXX", "secret-token"
FROM, TO = "918031700000", "919812345678"
REF = {"lead_id": "L1", "dial_at": "2026-10-05T06:30:00.123456Z"}
PLACED = {"api_id": "a1", "message": "call fired", "request_uuid": "RU-1"}
UNKNOWN = {"status": DIAL_OUTCOME_UNKNOWN, "sid": None}
DIAL_URL = f"https://api.plivo.com/v1/Account/{AUTH_ID}/Call/"


def _reply(status: int, body: Any = b"", headers=None):
    """A Plivo reply: a dict goes out as JSON, bytes as they are."""

    def make(request: requests.PreparedRequest) -> requests.Response:
        r = requests.models.Response()
        r.status_code = status
        r._content = json.dumps(body).encode() if isinstance(body, dict) else body
        r.headers = CaseInsensitiveDict(headers or {"Content-Type": "application/json"})
        r.url, r.request, r.encoding = str(request.url), request, "utf-8"
        return r

    return make


class FakeSend:
    """Stands in for requests.Session.send: records every request; each script item is
    a reply maker or an exception to raise, used in order (the last one repeats)."""

    def __init__(self, monkeypatch) -> None:
        self.requests: List[requests.PreparedRequest] = []
        self.script: List[Any] = [_reply(201, PLACED)]
        fake = self

        def send(session, request, **kw):
            fake.requests.append(request)
            item = fake.script.pop(0) if len(fake.script) > 1 else fake.script[0]
            if isinstance(item, BaseException):
                raise item
            return item(request)

        monkeypatch.setattr(requests.Session, "send", send)

    def body(self, i: int = -1) -> dict:
        return json.loads(cast(bytes, self.requests[i].body))


@pytest.fixture
def api(monkeypatch) -> FakeSend:
    return FakeSend(monkeypatch)


@pytest.fixture
def provider(monkeypatch) -> pv.PlivoProvider:
    """A provider as the dialler builds one: the environment's account."""
    monkeypatch.setattr(pv, "plivo_keys", lambda account: (AUTH_ID, TOKEN))
    p = pv.PlivoProvider(aiohttp_session=None)
    p.APP_BASE_URL = "https://app.example"
    return p


def _dial(provider: pv.PlivoProvider, **kw):
    return provider.make_call(TO, FROM, "R1", "T1", dial_ref=REF, **kw)


class _Log:
    """Records what plivo.py logs (loguru's logger does not take attribute patches)."""

    def __init__(self) -> None:
        self.lines: List[Tuple[str, str]] = []

    def __getattr__(self, level: str):
        return lambda message, *a, **k: self.lines.append((level, str(message)))


def test_the_sdk_is_the_audited_version():
    # Re-run the audit (docs/superpowers/specs/2026-10-05-plivo-sdk-audit.md) before
    # moving this pin: NoResendClient relies on client.py's request() as it is in 4.59.5.
    assert plivo_version == "4.59.5"


def test_the_stock_sdk_posts_a_dial_three_times_on_a_5xx(api, capsys):
    # why NoResendClient exists
    api.script = [_reply(503)]
    with pytest.raises(PlivoRestError):
        plivo.RestClient(AUTH_ID, TOKEN, timeout=2).calls.create(
            from_=FROM,
            to_=TO,
            answer_url="https://a.example/x",
            hangup_url="https://a.example/y",
        )
    assert len(api.requests) == 3
    assert {r.url for r in api.requests} == {DIAL_URL}
    assert "Fallback for URL" in capsys.readouterr().out


def test_the_provider_dials_and_transfers_on_a_no_resend_client(provider, api):
    assert isinstance(provider.client, pv.NoResendClient)
    assert provider.conference_service.client is provider.client
    api.script = [_reply(503)]
    with pytest.raises(PlivoRestError):
        provider.client.calls.transfer(
            "C1", legs="aleg", aleg_url="https://app.example/xfer"
        )
    assert len(api.requests) == 1


@pytest.mark.parametrize(
    "reply",
    [
        _reply(500, {"api_id": "a", "error": "internal error"}),
        _reply(502),
        _reply(503),
        _reply(504, b"<html>Gateway Timeout</html>", {"Content-Type": "text/html"}),
    ],
    ids=["500-with-body", "502", "503", "504-html"],
)
def test_every_5xx_is_sent_once_and_held(provider, api, capsys, reply):
    api.script = [reply]
    assert _dial(provider) == UNKNOWN
    assert len(api.requests) == 1
    assert "Fallback for URL" not in capsys.readouterr().out


def test_502_then_201_is_sent_once_and_held(provider, api):
    # the stock SDK would send again and keep the second call's id while the first rings
    api.script = [_reply(502), _reply(201, PLACED)]
    assert _dial(provider) == UNKNOWN
    assert len(api.requests) == 1


def test_a_2xx_with_a_request_uuid_is_placed_and_the_request_is_the_sdks(provider, api):
    assert _dial(provider) == {"status": "call_initiated", "sid": "RU-1"}
    (req,) = api.requests
    assert req.url == DIAL_URL
    assert req.headers["Authorization"] == requests.auth._basic_auth_str(AUTH_ID, TOKEN)
    body = api.body()
    assert (body["from"], body["to"]) == (FROM, TO)
    # the SDK's defaults still go out
    assert body["answer_method"] == "POST" and body["machine_detection_time"] == 5000
    assert body["hangup_url"].endswith(
        "?lead_id=L1&dial_at=2026-10-05T06%3A30%3A00.123456Z"
    )


@pytest.mark.parametrize(
    "reply",
    [
        _reply(201, b"OK", {"Content-Type": "text/plain"}),
        _reply(201, {"api_id": "a1", "message": "call fired"}),
    ],
    ids=["non-json", "api-id-only"],
)
def test_a_2xx_without_a_readable_request_uuid_is_held(provider, api, reply):
    api.script = [reply]
    assert _dial(provider) == UNKNOWN


def test_a_429_is_not_placed_and_throttled_only_when_asked(provider, api, monkeypatch):
    log = _Log()
    monkeypatch.setattr(pv, "logger", log)
    api.script = [
        _reply(
            429,
            {"api_id": "a", "error": "too many requests"},
            {"Content-Type": "application/json", "Retry-After": "2"},
        )
    ]
    assert _dial(provider) is None  # every caller but the v2 dialler: not placed
    assert _dial(provider, report_throttle=True) == {
        "status": DIAL_OUTCOME_THROTTLED,
        "sid": None,
        "retry_after_s": 2.0,
    }
    assert len(api.requests) == 2
    throttled = [m for level, m in log.lines if level == "warning" and "429" in m]
    assert len(throttled) == 2
    assert all(AUTH_ID in m and "L1" in m for m in throttled)


@pytest.mark.parametrize("retry_after", ["soon", "Wed, 21 Oct 2026 07:28:00 GMT"])
def test_a_retry_after_that_is_not_seconds_leaves_the_wait_to_the_loop(
    provider, api, retry_after
):
    api.script = [
        _reply(429, {"api_id": "a", "error": "x"}, {"Retry-After": retry_after})
    ]
    reply = _dial(provider, report_throttle=True)
    assert reply is not None and reply["retry_after_s"] is None


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_other_4xx_is_not_placed(provider, api, status):
    api.script = [_reply(status, {"api_id": "a", "error": "refused"})]
    assert _dial(provider) is None and len(api.requests) == 1


def test_an_argument_the_sdk_refuses_sends_nothing(provider, api):
    # from == to: the SDK raises ValidationError before any request
    assert provider.make_call(FROM, FROM, "R1", "T1", dial_ref=REF) is None
    assert api.requests == []


@pytest.mark.parametrize(
    "exc",
    [
        requests.exceptions.ConnectTimeout("connect timed out"),
        requests.exceptions.ConnectionError(
            urllib3.exceptions.NewConnectionError(
                cast(Any, None), "[Errno 111] Connection refused"
            )
        ),
        requests.exceptions.ConnectionError(
            socket.gaierror(-2, "Name or service not known")
        ),
    ],
    ids=["connect-timeout", "refused", "dns"],
)
def test_nothing_sent_is_not_placed(provider, api, exc):
    api.script = [exc]
    assert _dial(provider) is None


@pytest.mark.parametrize(
    "exc",
    [
        requests.exceptions.ReadTimeout("read timed out"),
        requests.exceptions.ConnectionError(ConnectionResetError(104, "reset by peer")),
        requests.exceptions.ChunkedEncodingError("broken chunk"),
        requests.exceptions.ContentDecodingError("broken gzip"),
    ],
    ids=["read-timeout", "reset", "chunked", "content-decoding"],
)
def test_sent_without_a_complete_reply_is_held(provider, api, exc):
    api.script = [exc]
    assert _dial(provider) == UNKNOWN


def test_body_decides_lets_a_5xx_with_plivos_body_count_as_not_placed(
    provider, api, monkeypatch
):
    monkeypatch.setattr(pv, "BB_PLIVO_5XX_OUTCOME", "body_decides")
    api.script = [_reply(500, {"api_id": "a", "error": "internal error"})]
    assert _dial(provider) is None
    api.script = [_reply(502)]
    assert _dial(provider) == UNKNOWN


def test_not_placed_is_todays_reading_without_the_re_send(provider, api, monkeypatch):
    monkeypatch.setattr(pv, "BB_PLIVO_5XX_OUTCOME", "not_placed")
    api.script = [_reply(502)]
    assert _dial(provider) is None and len(api.requests) == 1


async def test_make_call_async_carries_the_throttle_question_to_the_thread(
    provider, api
):
    api.script = [_reply(429, {"api_id": "a", "error": "x"})]
    reply = await provider.make_call_async(TO, FROM, dial_ref=REF, report_throttle=True)
    assert reply is not None and reply["status"] == DIAL_OUTCOME_THROTTLED
    assert await provider.make_call_async(TO, FROM, dial_ref=REF) is None


def test_an_argument_refused_after_an_earlier_reply_is_still_not_placed(provider, api):
    # The previous request's 429 must not answer for a request that never
    # left (the SDK refuses from == to before sending)
    api.script = [_reply(429, {"api_id": "a", "error": "x"})]
    throttled = _dial(provider, report_throttle=True)
    assert throttled is not None and throttled["status"] == DIAL_OUTCOME_THROTTLED
    refused = provider.make_call(
        FROM, FROM, "R1", "T1", dial_ref=REF, report_throttle=True
    )
    assert refused is None and len(api.requests) == 1


def test_a_surprise_after_plivo_replied_is_held(provider, api, monkeypatch):
    # An error that is neither the SDK's nor requests' once a reply is in:
    # Plivo may have placed the call
    def broken(self, method, response, response_type=None, objects_type=None):
        raise KeyError("parsing went wrong")

    monkeypatch.setattr(plivo.RestClient, "process_response", broken)
    assert _dial(provider) == UNKNOWN
    assert len(api.requests) == 1


def test_a_surprise_before_anything_was_sent_is_raised(provider, api, monkeypatch):
    def broken(self, *a, **k):
        raise KeyError("building the request went wrong")

    monkeypatch.setattr(plivo.RestClient, "create_request", broken)
    with pytest.raises(KeyError):
        _dial(provider)  # the dialler takes a raise as "not placed": nothing was sent
    assert api.requests == []
