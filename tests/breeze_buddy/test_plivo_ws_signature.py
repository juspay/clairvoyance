from plivo.utils.signature_v3 import get_signature_v3
from starlette.websockets import WebSocket

from app.ai.voice.agents.breeze_buddy.services.telephony.plivo import (
    security as plivo_security,
)

TOKEN = "test-plivo-auth-token"
PATH = "/agent/voice/breeze-buddy/plivo/callback/ws/v2"


async def _never(*args, **kwargs):
    raise AssertionError("not used")


def ws(signed_url: str, nonce: str = "n1") -> WebSocket:
    sig = get_signature_v3(TOKEN.encode(), signed_url, nonce.encode()).decode()
    headers = [
        (b"host", b"h.example"),
        (b"x-plivo-signature-v3", sig.encode()),
        (b"x-plivo-signature-v3-nonce", nonce.encode()),
    ]
    scope = {"type": "websocket", "path": PATH, "headers": headers}
    return WebSocket(scope, receive=_never, send=_never)


def test_ws_signature_direct_and_smart_router_pod(monkeypatch):
    monkeypatch.setattr(plivo_security, "PLIVO_AUTH_TOKEN", TOKEN)
    monkeypatch.setattr(plivo_security, "VOICE_AGENT_POD_NAME", "voice-agent-10")
    check = plivo_security.plivo_websocket_signature_ok

    assert check(ws(f"http://h.example{PATH}"))
    assert check(ws(f"http://h.example/ws/pod/voice-agent-10{PATH}"))
    assert not check(ws(f"http://h.example/ws/pod/voice-agent-11{PATH}"))
    assert not check(ws("http://other/x"))
