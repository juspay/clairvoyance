import base64
import hashlib
import hmac
from urllib.parse import urlencode

import pytest
from plivo.utils.signature_v3 import (
    construct_get_url,
    construct_post_url,
    get_signature_v3,
)
from starlette.requests import Request
from starlette.websockets import WebSocket

from app.ai.voice.agents.breeze_buddy.services.telephony import (
    signature_observe as obs,
)
from app.core.logger import logger

BASE = "https://cv.example"
VOBIZ_TOKEN = "test-vobiz-token"
PLIVO_TOKEN = "test-plivo-token"
STATUS = "/agent/voice/breeze-buddy/vobiz/callback/status"
WS = "/agent/voice/breeze-buddy/plivo/callback/ws/v2"


class FakeRedis:
    def __init__(self):
        self.keys = set()

    async def get_client(self):
        return self

    async def set(self, key, value, nx=False, ex=None):
        if key in self.keys:
            return None
        self.keys.add(key)
        return True


@pytest.fixture
def lines(monkeypatch):
    redis = FakeRedis()

    async def get_redis_service():
        return redis

    monkeypatch.setattr(obs, "get_redis_service", get_redis_service)
    monkeypatch.setattr(obs, "APP_BASE_URL", BASE)
    monkeypatch.setattr(obs, "VOBIZ_AUTH_TOKEN", VOBIZ_TOKEN)
    monkeypatch.setattr(obs, "PLIVO_AUTH_TOKEN", PLIVO_TOKEN)
    monkeypatch.setattr(obs, "VOICE_AGENT_POD_NAME", "voice-agent-10")
    out = []
    sink = logger.add(lambda m: out.append(m.record["message"]), level="INFO")
    yield out
    logger.remove(sink)


def post(path, form, headers, query=""):
    body = urlencode(form).encode()
    raw = [(b"content-type", b"application/x-www-form-urlencoded")]
    raw += [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "query_string": query.encode(),
        "headers": raw,
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def vobiz_headers(url, nonce):
    digest = hmac.new(
        VOBIZ_TOKEN.encode(), f"{url}.{nonce}".encode(), hashlib.sha256
    ).digest()
    return {
        "X-Vobiz-Signature-V3": base64.b64encode(digest).decode(),
        "X-Vobiz-Signature-V3-Nonce": nonce,
    }


async def test_vobiz_webhook_valid_replay_forged_missing(lines):
    form = {"CallUUID": "c-111", "Event": "Hangup"}
    signed = vobiz_headers(BASE + STATUS, "48291577306113922051")

    await obs.observe_webhook_signature(post(STATUS, form, signed), "vobiz", "status")
    await obs.observe_webhook_signature(post(STATUS, form, signed), "vobiz", "status")
    forged = dict(signed, **{"X-Vobiz-Signature-V3-Nonce": "48291577306113922099"})
    await obs.observe_webhook_signature(post(STATUS, form, forged), "vobiz", "status")
    await obs.observe_webhook_signature(post(STATUS, form, {}), "vobiz", "status")

    assert "present=true valid=true replay=false" in lines[0]
    assert "call=c-111" in lines[0]
    assert "present=true valid=true replay=true" in lines[1]
    assert "present=true valid=false replay=-" in lines[2]
    assert "present=false valid=false replay=-" in lines[3]
    assert not any("48291577306113922051" in line for line in lines)


async def test_vobiz_signature_ignores_query(lines):
    form = {"CallUUID": "c-111", "Event": "RecordStop"}
    path = "/agent/voice/breeze-buddy/vobiz/callback/details"
    signed = vobiz_headers(BASE + path, "90551273648820419376")
    request = post(path, form, signed, query="call_uuid=c-111")

    await obs.observe_webhook_signature(request, "vobiz", "details")

    assert "valid=true" in lines[0]


async def test_plivo_webhook_signs_query_and_form(lines):
    path = "/agent/voice/breeze-buddy/plivo/answer"
    form = {"CallUUID": "p-1", "From": "1", "To": "2"}
    url = f"{BASE}{path}?x=1"
    signed_text = construct_post_url(url, form).decode()
    nonce = "12345678901234567890"
    signature = get_signature_v3(
        PLIVO_TOKEN.encode(), signed_text, nonce.encode()
    ).decode()
    headers = {"X-Plivo-Signature-V3": signature, "X-Plivo-Signature-V3-Nonce": nonce}

    await obs.observe_webhook_signature(
        post(path, form, headers, "x=1"), "plivo", "answer"
    )
    tampered = dict(form, From="9")
    await obs.observe_webhook_signature(
        post(path, tampered, headers, "x=1"), "plivo", "answer"
    )

    assert "provider=plivo route=answer" in lines[0]
    assert "valid=true replay=false" in lines[0]
    assert "valid=false" in lines[1]


async def test_plivo_get_transfer_signs_query(lines):
    path = "/agent/voice/breeze-buddy/plivo/callback/transfer/dial-up"
    query = "customer_call_sid=p-2&CallUUID=p-2&Event=Redirect"
    url = f"{BASE}{path}?{query}"
    nonce = "55554444333322221111"
    signature = get_signature_v3(
        PLIVO_TOKEN.encode(), construct_get_url(url, {}), nonce.encode()
    ).decode()
    headers = [
        (b"x-plivo-signature-v3", signature.encode()),
        (b"x-plivo-signature-v3-nonce", nonce.encode()),
    ]
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": query.encode(),
        "headers": headers,
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await obs.observe_webhook_signature(
        Request(scope, receive), "plivo", "transfer/dial-up"
    )

    assert "route=transfer/dial-up method=GET present=true valid=true" in lines[0]
    assert "call=p-2" in lines[0]


def ws(signed_url, nonce="n1"):
    signature = get_signature_v3(PLIVO_TOKEN.encode(), signed_url, nonce.encode())
    headers = [
        (b"host", b"cv.example"),
        (b"x-plivo-signature-v3", signature),
        (b"x-plivo-signature-v3-nonce", nonce.encode()),
    ]
    scope = {"type": "websocket", "path": WS, "headers": headers}

    async def never(*args, **kwargs):
        raise AssertionError("not used")

    return WebSocket(scope, receive=never, send=never)


async def test_plivo_ws_direct_and_smart_router_pod(lines):
    await obs.observe_websocket_signature(ws(f"http://cv.example{WS}", "a"), "plivo")
    await obs.observe_websocket_signature(
        ws(f"http://cv.example/ws/pod/voice-agent-10{WS}", "b"), "plivo"
    )
    await obs.observe_websocket_signature(
        ws(f"http://cv.example/ws/pod/voice-agent-11{WS}", "c"), "plivo"
    )

    assert "valid=true matched=direct" in lines[0]
    assert "valid=true matched=pod" in lines[1]
    assert "valid=false matched=none" in lines[2]


async def test_redis_error_logs_replay_error(lines, monkeypatch):
    async def broken_redis():
        raise ConnectionError("redis down")

    monkeypatch.setattr(obs, "get_redis_service", broken_redis)
    form = {"CallUUID": "c-111", "Event": "Hangup"}
    signed = vobiz_headers(BASE + STATUS, "11112222333344445555")

    await obs.observe_webhook_signature(post(STATUS, form, signed), "vobiz", "status")

    assert "telephony_signature nonce check failed: ConnectionError" in lines[0]
    assert "present=true valid=true replay=error" in lines[1]


async def test_other_providers_are_ignored(lines):
    await obs.observe_webhook_signature(post(STATUS, {}, {}), "exotel", "status")

    assert lines == []
