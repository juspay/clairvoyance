"""
Tests for a Vobiz call's audio stream (Vobiz phase 2).

Vobiz's bidirectional <Stream> speaks Plivo's websocket dialect, so pipecat
auto-detects every Vobiz call as "plivo". Left alone, such a call hangs up
through api.plivo.com with the PLIVO_* keys: the hang-up fails, the customer's
line stays open and the Vobiz channel keeps billing. What is proven here:

- VobizFrameSerializer is pipecat's Plivo serializer with ONLY the REST
  hang-up replaced: one DELETE to Vobiz with X-Auth-ID / X-Auth-Token, bounded
  by 10 s, never following a redirect (the X-Auth headers must not reach
  another host), refusing a call id that is not UUID-shaped (it comes from the
  unauthenticated start event), and never raising into pipeline teardown;
- the serializer a Vobiz call gets speaks the Vobiz stream shapes (playAudio
  μ-law 8 kHz out, media in);
- the agent relabels a VOBIZ call "vobiz" so the transport helper builds that
  serializer, at the first build and at the agent-to-agent rebuild, while
  plivo / twilio / exotel still go to pipecat's own builder unchanged;
- the pre-pipeline greeting takes the Plivo playAudio / μ-law path for Vobiz.

Hermetic: the hang-up talks to local aiohttp servers on 127.0.0.1, and DNS is
refused for every aiohttp client in this file, so a regression that points the
hang-up back at api.plivo.com fails here instead of reaching the internet.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple, cast

import aiohttp.connector
import pytest
from aiohttp import web
from aiohttp.abc import AbstractResolver
from fastapi import WebSocket
from loguru import logger
from multidict import CIMultiDict
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    StartFrame,
)
from pipecat.serializers.plivo import PlivoFrameSerializer
from pipecat.transports.websocket.fastapi import FastAPIWebsocketTransport

from app.ai.voice.agents.breeze_buddy import agent as agent_mod
from app.ai.voice.agents.breeze_buddy.agent import (
    transfer as transfer_mod,
    transport as transport_mod,
    utils as agent_utils,
)
from app.ai.voice.agents.breeze_buddy.agent.utils import GreetingResult
from app.ai.voice.agents.breeze_buddy.services.telephony.vobiz import (
    serializer as vz_serializer,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.vobiz.serializer import (
    VobizFrameSerializer,
)
from app.ai.voice.agents.breeze_buddy.utils import common as common_mod
from app.ai.voice.agents.breeze_buddy.utils.agent_transfer import (
    PendingAgentTransfer,
)
from app.core.config import static
from app.schemas import CallProvider
from app.schemas.breeze_buddy.core import (
    CallDirection,
    ExecutionMode,
    LeadCallStatus,
    LeadCallTracker,
)

AUTH_ID = "MA_VOBIZTEST"
AUTH_TOKEN = "vobiz-test-token"
CALL_ID = "5401fd2e-6344-40df-a22c-c8ffea7a92e7"
STREAM_ID = "c4dfd815-a92a-4140-ab85-5ff28c004116"

# The opening messages of a Vobiz stream, as on
# https://www.vobiz.ai/docs/xml/stream/stream-events (payload made real μ-law).
VOBIZ_START = {
    "sequenceNumber": 0,
    "event": "start",
    "start": {
        "callId": CALL_ID,
        "streamId": STREAM_ID,
        "accountId": "500025",
        "tracks": ["inbound"],
        "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000},
    },
    "extra_headers": "{}",
}
VOBIZ_MEDIA = {
    "sequenceNumber": 2,
    "streamId": STREAM_ID,
    "event": "media",
    "media": {
        "track": "inbound",
        "timestamp": "1778597597091",
        "chunk": 2,
        "payload": base64.b64encode(b"\xff" * 160).decode(),
    },
    "extra_headers": "{}",
}


# ── hermetic guard + fakes ───────────────────────────────────────────────


class _RefuseDNS(AbstractResolver):
    """Every aiohttp client in this file may reach IP literals only."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> List[Any]:
        raise OSError(f"hermetic test: refused DNS lookup of {host}")

    async def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    monkeypatch.setattr(aiohttp.connector, "DefaultResolver", _RefuseDNS)


@pytest.fixture
def logs():
    records: List[Dict[str, Any]] = []
    sink = logger.add(lambda message: records.append(message.record), level="DEBUG")
    yield records
    logger.remove(sink)


def messages(records: List[Dict[str, Any]], level: str) -> List[str]:
    return [r["message"] for r in records if r["level"].name == level]


class FakeApi:
    """A local HTTP server that records every request and answers as told."""

    def __init__(self, status: int = 204, body: str = "") -> None:
        self.status = status
        self.body = body
        self.location: Optional[str] = None
        self.requests: List[Dict[str, Any]] = []
        self.base = ""
        self._runner: Optional[web.AppRunner] = None

    async def _handle(self, request: web.Request) -> web.Response:
        self.requests.append(
            {
                "method": request.method,
                "host": request.host,
                "path": request.path,
                "headers": CIMultiDict(request.headers),
                "body": await request.read(),
            }
        )
        headers = {"Location": self.location} if self.location else None
        return web.Response(status=self.status, text=self.body or None, headers=headers)

    async def start(self) -> "FakeApi":
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", 0).start()
        self.base = f"http://127.0.0.1:{self._runner.addresses[0][1]}"
        return self

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()


@pytest.fixture
async def vobiz_api(monkeypatch):
    """A fake Vobiz REST API; the serializer's base URL points at it."""
    api = await FakeApi().start()
    monkeypatch.setattr(vz_serializer, "VOBIZ_API_BASE_URL", f"{api.base}/api/v1")
    monkeypatch.setattr(vz_serializer, "get_proxy_config", lambda: None)
    yield api
    await api.stop()


def make_serializer(**overrides: Any) -> VobizFrameSerializer:
    kwargs: Dict[str, Any] = {
        "stream_id": STREAM_ID,
        "call_id": CALL_ID,
        "auth_id": AUTH_ID,
        "auth_token": AUTH_TOKEN,
    }
    kwargs.update(overrides)
    return VobizFrameSerializer(**kwargs)


HANGUP_PATH = f"/api/v1/Account/{AUTH_ID}/Call/{CALL_ID}/"


# ── the serializer: what it is ───────────────────────────────────────────


def test_vobiz_serializer_is_plivo_s_with_only_the_hang_up_replaced():
    """Every frame Vobiz exchanges is Plivo's (start / media in, playAudio /
    clearAudio out), so frames stay pipecat's code and only the REST hang-up
    is ours. A second override would fork pipecat's frame handling; losing
    this one sends the hang-up to api.plivo.com."""
    own_methods = {
        name for name, value in vars(VobizFrameSerializer).items() if callable(value)
    }
    assert VobizFrameSerializer.__bases__ == (PlivoFrameSerializer,)
    assert own_methods == {"_hang_up_call"}


# ── the serializer: the REST hang-up ─────────────────────────────────────


async def test_end_of_call_sends_exactly_one_delete_to_vobiz_with_its_auth_headers(
    vobiz_api,
):
    """https://www.vobiz.ai/docs/call/hangup-call:
    DELETE https://api.vobiz.ai/api/v1/Account/{auth_id}/Call/{call_uuid}/
    with X-Auth-ID / X-Auth-Token, no body, 204 on success. Plivo's Basic auth
    must not ride along, and an EndFrame followed by a CancelFrame (both reach
    the serializer at teardown) must hang up once, not twice."""
    serializer = make_serializer()

    assert await serializer.serialize(EndFrame()) is None
    assert await serializer.serialize(CancelFrame()) is None

    assert len(vobiz_api.requests) == 1
    sent = vobiz_api.requests[0]
    assert sent["method"] == "DELETE"
    assert sent["path"] == HANGUP_PATH
    assert sent["headers"]["X-Auth-ID"] == AUTH_ID
    assert sent["headers"]["X-Auth-Token"] == AUTH_TOKEN
    assert "Authorization" not in sent["headers"]
    assert sent["body"] == b""


@pytest.mark.parametrize(
    "status,level,expected",
    [
        (204, "DEBUG", f"Successfully terminated Vobiz call {CALL_ID}"),
        # Not on the Vobiz page; kept from Plivo: the call already ended.
        (404, "DEBUG", f"Vobiz call {CALL_ID} already terminated"),
        (401, "ERROR", "Status 401, Response: vobiz said no"),
        (500, "ERROR", "Status 500, Response: vobiz said no"),
        (503, "ERROR", "Status 503, Response: vobiz said no"),
    ],
)
async def test_hang_up_outcomes_are_logged_and_never_raised(
    vobiz_api, logs, status, level, expected
):
    """The hang-up runs inside the output transport's stop(): an exception
    here would break pipeline teardown. 204 and 404 mean the line is down and
    stay quiet; anything else is an ERROR carrying Vobiz's answer."""
    vobiz_api.status = status
    vobiz_api.body = "" if status == 204 else "vobiz said no"

    assert await make_serializer().serialize(EndFrame()) is None

    assert any(expected in m for m in messages(logs, level))
    if level == "DEBUG":
        assert messages(logs, "ERROR") == []


@pytest.mark.parametrize("status", [301, 302, 307])
async def test_hang_up_never_follows_a_redirect(vobiz_api, logs, status):
    """aiohttp strips only Authorization on a cross-origin redirect, so a
    followed 3xx would hand X-Auth-ID / X-Auth-Token to whatever host the
    Location names. The DELETE is sent with allow_redirects=False: the other
    host sees nothing, and the 3xx is logged as a failed hang-up."""
    elsewhere = await FakeApi().start()
    try:
        vobiz_api.status = status
        vobiz_api.location = f"{elsewhere.base}/steal"

        assert await make_serializer().serialize(EndFrame()) is None
    finally:
        await elsewhere.stop()

    assert len(vobiz_api.requests) == 1
    assert elsewhere.requests == []
    assert any(f"Status {status}" in m for m in messages(logs, "ERROR"))


@pytest.mark.parametrize(
    "call_id", ["../numbers/+919800000000?", "a/b", "x?y", "%2e%2e", ""]
)
async def test_a_call_id_that_is_not_uuid_shaped_never_reaches_the_url(
    vobiz_api, logs, call_id
):
    """The call id comes from the websocket's unauthenticated start event and
    is spliced into an X-Auth-signed URL. yarl collapses "..", so
    "../numbers/+919800000000?" would become an authenticated
    DELETE /api/v1/Account/{auth_id}/numbers/+919800000000. Anything but
    letters, digits and hyphens is refused before a request is made (real
    CallUUIDs are UUIDs: the end-of-call test above sends one)."""
    serializer = make_serializer()
    # Set after construction: "" would not get past pipecat's __init__.
    serializer._call_id = call_id

    assert await serializer.serialize(EndFrame()) is None

    assert vobiz_api.requests == []
    assert any(
        f"unexpected call id: {call_id!r}" in m for m in messages(logs, "WARNING")
    )


async def test_unreachable_vobiz_is_logged_with_the_exception_repr(monkeypatch, logs):
    """A refused connection is caught and logged by type, not raised."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]
    monkeypatch.setattr(
        vz_serializer, "VOBIZ_API_BASE_URL", f"http://127.0.0.1:{closed_port}/api/v1"
    )
    monkeypatch.setattr(vz_serializer, "get_proxy_config", lambda: None)

    assert await make_serializer().serialize(EndFrame()) is None

    assert any(
        f"Failed to hang up Vobiz call {CALL_ID}: ClientConnectorError(" in m
        for m in messages(logs, "ERROR")
    )


async def test_a_black_holed_vobiz_cannot_hold_teardown(monkeypatch, logs):
    """A request that is accepted and never answered would hold the pipeline's
    stop() for aiohttp's 300 s default. The hang-up is bounded (10 s in
    production, shrunk here), and the timeout is logged by repr, since
    str(TimeoutError()) is empty and the log line would say nothing."""
    assert vz_serializer._HANGUP_TIMEOUT_SECONDS == 10

    release = asyncio.Event()

    async def never_answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        await reader.read(65536)
        await release.wait()
        writer.close()

    server = await asyncio.start_server(never_answer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(
        vz_serializer, "VOBIZ_API_BASE_URL", f"http://127.0.0.1:{port}/api/v1"
    )
    monkeypatch.setattr(vz_serializer, "get_proxy_config", lambda: None)
    monkeypatch.setattr(vz_serializer, "_HANGUP_TIMEOUT_SECONDS", 0.3)
    try:
        result = await asyncio.wait_for(make_serializer().serialize(EndFrame()), 5)
    finally:
        release.set()
        server.close()
        await server.wait_closed()

    assert result is None
    assert any(
        f"Failed to hang up Vobiz call {CALL_ID}: TimeoutError()" in m
        for m in messages(logs, "ERROR")
    )


async def test_hang_up_leaves_through_the_egress_proxy(monkeypatch):
    """On AWS pods the only way out is the egress proxy that get_proxy_config
    names; a hang-up that skips it never reaches Vobiz. The local server plays
    the proxy here: it receives the DELETE addressed to the Vobiz host."""
    proxy = await FakeApi().start()
    monkeypatch.setattr(
        vz_serializer, "VOBIZ_API_BASE_URL", "http://api.vobiz.invalid/api/v1"
    )
    monkeypatch.setattr(vz_serializer, "get_proxy_config", lambda: proxy.base)
    try:
        assert await make_serializer().serialize(EndFrame()) is None
    finally:
        await proxy.stop()

    assert len(proxy.requests) == 1
    assert proxy.requests[0]["host"] == "api.vobiz.invalid"
    assert proxy.requests[0]["path"] == HANGUP_PATH


# ── the transport a Vobiz call gets ──────────────────────────────────────


@pytest.fixture
def vobiz_keys(monkeypatch):
    """create_telephony_transport reads static.VOBIZ_* at call time."""
    monkeypatch.setattr(static, "VOBIZ_AUTH_ID", AUTH_ID)
    monkeypatch.setattr(static, "VOBIZ_AUTH_TOKEN", AUTH_TOKEN)


@pytest.fixture
def pipecat_builder(monkeypatch):
    """Spy on pipecat's telephony factory where the helper looks it up."""
    calls: List[Tuple[Any, ...]] = []
    built = object()

    async def fake_builder(websocket, params, transport_type, call_data):
        calls.append((websocket, params, transport_type, call_data))
        return built

    monkeypatch.setattr(transport_mod, "_create_telephony_transport", fake_builder)
    return SimpleNamespace(calls=calls, built=built)


def test_vobiz_transport_params_are_the_plivo_telephony_params():
    """Both legs of a Vobiz stream are 8 kHz, like Plivo's."""
    factories = transport_mod.get_transport_params()
    vobiz, plivo = factories["vobiz"](), factories["plivo"]()
    for field in (
        "audio_in_enabled",
        "audio_out_enabled",
        "audio_in_sample_rate",
        "audio_out_sample_rate",
    ):
        assert getattr(vobiz, field) == getattr(plivo, field), field
    assert vobiz.audio_in_sample_rate == vobiz.audio_out_sample_rate == 8000


async def test_vobiz_transport_hangs_up_through_vobiz_with_the_vobiz_keys(
    vobiz_keys, vobiz_api, pipecat_builder
):
    """A "vobiz" call never reaches pipecat's factory (which would build a
    Plivo serializer on PLIVO_* keys); it gets VobizFrameSerializer on the
    call's stream / call ids and the VOBIZ_* keys, and no WAV header."""
    params = transport_mod.get_transport_params()["vobiz"]().model_copy(
        update={"add_wav_header": True}
    )
    call_data = {"stream_id": STREAM_ID, "call_id": CALL_ID}

    transport = await transport_mod.create_telephony_transport(
        cast(WebSocket, object()), params, "vobiz", call_data
    )

    assert pipecat_builder.calls == []
    assert isinstance(transport, FastAPIWebsocketTransport)
    assert params.add_wav_header is False
    serializer = params.serializer
    assert type(serializer) is VobizFrameSerializer
    assert transport._params is params

    assert await serializer.serialize(EndFrame()) is None
    assert [r["path"] for r in vobiz_api.requests] == [HANGUP_PATH]
    assert vobiz_api.requests[0]["headers"]["X-Auth-Token"] == AUTH_TOKEN


async def test_vobiz_transport_speaks_the_vobiz_stream_protocol(vobiz_keys):
    """https://www.vobiz.ai/docs/xml/stream/play-audio (μ-law at 8 kHz):
    {"event": "playAudio", "streamId": ..., "media": {"contentType":
    "audio/x-mulaw", "sampleRate": 8000, "payload": <base64>}}; inbound audio
    is a "media" event (https://www.vobiz.ai/docs/xml/stream/stream-events)."""
    params = transport_mod.get_transport_params()["vobiz"]()
    await transport_mod.create_telephony_transport(
        cast(WebSocket, object()),
        params,
        "vobiz",
        {"stream_id": STREAM_ID, "call_id": CALL_ID},
    )
    serializer = params.serializer
    assert serializer is not None
    await serializer.setup(
        StartFrame(audio_in_sample_rate=8000, audio_out_sample_rate=8000)
    )

    out = await serializer.serialize(
        OutputAudioRawFrame(audio=b"\x00\x00" * 160, sample_rate=8000, num_channels=1)
    )
    assert isinstance(out, str)
    message = json.loads(out)
    payload = message["media"].pop("payload")
    assert message == {
        "event": "playAudio",
        "streamId": STREAM_ID,
        "media": {"contentType": "audio/x-mulaw", "sampleRate": 8000},
    }
    assert len(base64.b64decode(payload)) == 160  # one μ-law byte per sample

    frame = await serializer.deserialize(json.dumps(VOBIZ_MEDIA))
    assert isinstance(frame, InputAudioRawFrame)
    assert frame.sample_rate == 8000
    assert len(frame.audio) == 320  # 160 samples of 16-bit PCM


@pytest.mark.parametrize("transport_type", ["plivo", "twilio", "exotel"])
async def test_other_providers_still_get_pipecat_s_own_builder(
    pipecat_builder, transport_type
):
    params = transport_mod.get_transport_params()[transport_type]()
    call_data = {"stream_id": STREAM_ID, "call_id": CALL_ID}
    ws = cast(WebSocket, object())

    transport = await transport_mod.create_telephony_transport(
        ws, params, transport_type, call_data
    )

    assert transport is pipecat_builder.built
    assert pipecat_builder.calls == [(ws, params, transport_type, call_data)]
    assert params.serializer is None


# ── the relabel: a VOBIZ call is not a Plivo call ────────────────────────


class FakeProviderSocket:
    """The provider's websocket, replaying the stream's opening messages."""

    def __init__(self, opening: List[Dict[str, Any]]) -> None:
        self._opening = [json.dumps(m) for m in opening]
        self.query_params: Dict[str, str] = {}

    async def accept(self) -> None:
        return None

    async def iter_text(self):
        for message in self._opening:
            yield message


def make_lead() -> LeadCallTracker:
    return LeadCallTracker(
        id="lead-vz-1",
        telephony_number_id="num-1",
        reseller_id="res-1",
        template="welcome",
        status=LeadCallStatus.PROCESSING,
        call_id=CALL_ID,
        call_direction=CallDirection.OUTBOUND,
        execution_mode=ExecutionMode.TELEPHONY,
    )


@pytest.fixture
def call_setup(monkeypatch, vobiz_keys):
    """Everything _setup_telephony_transport touches besides the transport
    choice, patched where the agent looks it up (no Redis, DB or TTS)."""
    monkeypatch.setenv("PLIVO_AUTH_ID", "MA_PLIVOTEST")
    monkeypatch.setenv("PLIVO_AUTH_TOKEN", "plivo-test-token")
    template = SimpleNamespace(id="tpl-1", name="welcome", configurations=None)

    async def no_block_redirect(call_sid):
        return None

    async def lead_for(call_sid, when):
        return make_lead()

    async def load_template(lead):
        return template, None, {}

    async def nothing(**kwargs):
        return None

    async def no_greeting(**kwargs):
        return GreetingResult(source=None, text=None)

    async def no_vad(**kwargs):
        return None, None

    monkeypatch.setattr(agent_mod, "get_block_redirect", no_block_redirect)
    monkeypatch.setattr(agent_mod, "update_lead_call_initiated_time", lead_for)
    monkeypatch.setattr(agent_mod, "load_template_config", load_template)
    monkeypatch.setattr(agent_mod, "prepare_and_store_initial_greeting", nothing)
    monkeypatch.setattr(agent_mod, "send_initial_greeting", no_greeting)
    monkeypatch.setattr(agent_mod, "create_vad_analyzer", no_vad)
    monkeypatch.setattr(transfer_mod, "create_vad_analyzer", no_vad)
    monkeypatch.setattr(transfer_mod, "update_lead_template", nothing)

    async def set_up(provider: CallProvider) -> agent_mod.Agent:
        ws = FakeProviderSocket([VOBIZ_START, VOBIZ_MEDIA])
        agent = agent_mod.Agent(
            transport_type=provider, ws=cast(WebSocket, ws), provider=provider
        )
        assert await agent._setup_telephony_transport() is True
        return agent

    return set_up


@pytest.mark.parametrize(
    "provider,transport_type,serializer_cls,auth_id",
    [
        (CallProvider.VOBIZ, "vobiz", VobizFrameSerializer, AUTH_ID),
        (CallProvider.PLIVO, "plivo", PlivoFrameSerializer, "MA_PLIVOTEST"),
    ],
)
async def test_the_websocket_path_not_pipecat_decides_the_serializer(
    call_setup, provider, transport_type, serializer_cls, auth_id
):
    """THE TRAP: a Vobiz start event carries start.streamId + start.callId,
    which is exactly what pipecat's auto-detect calls "plivo". The same
    opening, arriving on the /vobiz websocket (provider VOBIZ), must build the
    Vobiz serializer on the VOBIZ_* keys; on the /plivo websocket it must
    still build pipecat's own Plivo serializer on the PLIVO_* keys."""
    agent = await call_setup(provider)

    assert agent.call_sid == CALL_ID and agent.stream_sid == STREAM_ID
    assert agent._rebuild.telephony_transport_type == transport_type
    serializer = agent.transport._params.serializer
    assert type(serializer) is serializer_cls
    assert serializer._auth_id == auth_id


async def test_an_agent_to_agent_transfer_keeps_the_vobiz_serializer(call_setup):
    """The transfer builds a fresh transport over the same websocket; it goes
    through the same helper, so generation 2 still hangs up through Vobiz
    (pipecat's factory would refuse "vobiz" outright)."""
    agent = await call_setup(CallProvider.VOBIZ)
    first = agent.transport
    target = SimpleNamespace(id="tpl-2", name="billing", configurations=None)

    await transfer_mod.apply_transfer(
        agent, PendingAgentTransfer(template=cast(Any, target), template_vars={})
    )

    assert agent.transport is not first
    assert isinstance(agent.transport, FastAPIWebsocketTransport)
    assert type(agent.transport._params.serializer) is VobizFrameSerializer


# ── the greeting sent before the pipeline starts ─────────────────────────


PLAY_AUDIO = {
    "event": "playAudio",
    "streamId": STREAM_ID,
    "media": {"contentType": "audio/x-mulaw", "sampleRate": 8000, "payload": "QUJD"},
}
TWILIO_MEDIA = {"event": "media", "streamSid": STREAM_ID, "media": {"payload": "QUJD"}}


@pytest.mark.parametrize(
    "provider,expected",
    [
        (CallProvider.PLIVO, PLAY_AUDIO),
        (CallProvider.VOBIZ, PLAY_AUDIO),
        (CallProvider.TWILIO, TWILIO_MEDIA),
        (CallProvider.EXOTEL, TWILIO_MEDIA),
    ],
)
async def test_greeting_goes_out_in_the_provider_s_stream_message(
    monkeypatch, provider, expected
):
    """https://www.vobiz.ai/docs/xml/stream/play-audio: Vobiz plays only
    playAudio messages; a Twilio-style "media" message would be dropped and
    the customer would hear silence until the agent's first turn."""
    sent: List[Dict[str, Any]] = []

    async def fake_payload(lead, template, provider):
        return {"payload": "QUJD", "greeting_source": "template_static"}

    async def fake_send(ws, message):
        sent.append(message)
        return True

    monkeypatch.setattr(agent_utils, "prepare_initial_greeting_payload", fake_payload)
    monkeypatch.setattr(agent_utils, "send_message", fake_send)

    result = await agent_utils.send_initial_greeting(
        ws=cast(WebSocket, object()),
        stream_sid=STREAM_ID,
        lead=make_lead(),
        template=cast(Any, SimpleNamespace()),
        provider=provider,
    )

    assert sent == [expected]
    assert result.source == "template_static"


class FakeRedis:
    def __init__(self, values: Dict[str, str]) -> None:
        self.values = values

    async def get(self, key: str) -> Optional[str]:
        return self.values.get(key)

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)


MULAW = bytes(range(0, 256, 2)) + bytes(range(1, 64, 2))  # 160 μ-law bytes


@pytest.mark.parametrize(
    "provider,stays_mulaw",
    [
        (CallProvider.PLIVO, True),
        (CallProvider.VOBIZ, True),
        (CallProvider.TWILIO, True),
        (CallProvider.EXOTEL, False),
    ],
)
async def test_greeting_audio_stays_mulaw_for_vobiz(monkeypatch, provider, stays_mulaw):
    """Vobiz's playAudio takes audio/x-mulaw at 8 kHz; only Exotel wants raw
    16-bit PCM, which is twice the bytes and noise if played as μ-law."""
    redis = FakeRedis({"greeting:template:tpl-1": base64.b64encode(MULAW).decode()})

    async def fake_redis():
        return redis

    monkeypatch.setattr(common_mod, "get_redis_service", fake_redis)

    result = await common_mod.prepare_initial_greeting_payload(
        lead=SimpleNamespace(id="lead-vz-1"),
        template=SimpleNamespace(id="tpl-1", configurations=None),
        provider=provider,
    )

    assert result is not None
    audio = base64.b64decode(result["payload"])
    if stays_mulaw:
        assert audio == MULAW
    else:
        assert len(audio) == 2 * len(MULAW)
