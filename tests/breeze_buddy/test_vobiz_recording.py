"""
Tests for Vobiz session recordings.

A Vobiz call is recorded by a ``<Record/>`` placed before the ``<Stream>`` in
the answer XML. Vobiz's defaults would hurt every call (a beep before the
greeting, a 60 s length cap and a 60 s silence cut-off), so the element must
override them exactly. The RecordStop callback carries an unauthenticated URL,
so the Vobiz X-Auth credentials may only ever go to a Vobiz host over https
(or to the configured API host), and never follow a redirect to another
origin. Vobiz can serve WAV bytes under an .mp3 URL, so the GCS object is named
from the bytes. And the console plays a VOBIZ recording back through the same
authenticated download.

Nothing here reaches Vobiz, GCS, Postgres or Redis: HTTP downloads run against
local aiohttp servers on 127.0.0.1, everything else is monkeypatched.
"""

from __future__ import annotations

import asyncio
import time
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from io import BytesIO
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from aiohttp import web
from fastapi import BackgroundTasks
from starlette.requests import Request

# dispatch must import first: managers.calls and dispatch.worker import each
# other, and only this order resolves it (the order the app itself loads them
# in). "...breeze_buddy" sorts before "...breeze_buddy.managers", so isort
# keeps this line above the next one on its own.
from app.ai.voice.agents.breeze_buddy import dispatch as _dispatch  # noqa: F401
from app.ai.voice.agents.breeze_buddy.managers import calls as calls_mod
from app.ai.voice.agents.breeze_buddy.services.telephony.plivo import (
    recording as plivo_rec,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.vobiz import (
    recording as rec,
)
from app.api.routers.breeze_buddy.leads import handlers as leads_mod
from app.api.routers.breeze_buddy.telephony.answer import handlers as ans_mod
from app.api.routers.breeze_buddy.telephony.callbacks import handlers as cb_mod
from app.schemas import CallProvider, ExecutionMode, LeadCallStatus
from app.schemas.breeze_buddy.auth import UserInfo, UserRole
from app.schemas.breeze_buddy.core import (
    LeadCallTracker,
    TelephonyNumber,
    TelephonyNumberStatus,
)

APP_BASE = "https://bb.example.test"
AUTH_ID = "MA_TEST_ID"
AUTH_TOKEN = "test-token-123"
CREDS = {"X-Auth-ID": AUTH_ID, "X-Auth-Token": AUTH_TOKEN}
LIMIT = 7200
CALLBACK_PATH = "/agent/voice/breeze-buddy/vobiz/callback/details"
# The configured API base (VOBIZ_API_BASE_URL) is pinned to this stand-in, so
# the local servers below are "the Vobiz API host" and nothing else is.
API_HOST = "127.0.0.1"
# Another origin: reaches the same loopback, but by a different host name.
OTHER_HOST = "localhost"

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 20
ID3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 22
MPEG_FRAME = b"\xff\xfb\x90\x64" + b"\x00" * 28

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@pytest.fixture(autouse=True)
def vobiz_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Static config is frozen at import: pin every value the code reads."""
    monkeypatch.setattr(rec, "VOBIZ_AUTH_ID", AUTH_ID)
    monkeypatch.setattr(rec, "VOBIZ_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(rec, "VOBIZ_RECORDING_TIME_LIMIT", LIMIT)
    monkeypatch.setattr(rec, "_VOBIZ_API_HOST", API_HOST)
    monkeypatch.setattr(rec, "_VOBIZ_API_SCHEME", "http")
    monkeypatch.setattr(rec, "get_proxy_config", lambda: None)
    monkeypatch.setattr(rec, "APP_BASE_URL", APP_BASE)
    monkeypatch.setattr(plivo_rec, "APP_BASE_URL", APP_BASE)
    monkeypatch.setattr(plivo_rec, "PLIVO_RECORDING_TIME_LIMIT", 14400)
    monkeypatch.setattr(ans_mod, "APP_BASE_URL", APP_BASE)


def set_noise_cancellation(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, level: str = "high"
) -> None:
    async def nc_enabled() -> bool:
        return enabled

    async def nc_level() -> str:
        return level

    monkeypatch.setattr(ans_mod, "BB_NOISE_CANCELLATION_ENABLED", nc_enabled)
    monkeypatch.setattr(ans_mod, "BB_NOISE_CANCELLATION_LEVEL", nc_level)


def xauth(request: web.Request) -> Dict[str, str]:
    return {k: v for k, v in request.headers.items() if k.lower().startswith("x-auth")}


@asynccontextmanager
async def serve(routes: Dict[str, Handler]) -> AsyncIterator[int]:
    """A local HTTP server on 127.0.0.1:<ephemeral>; yields its port."""
    app = web.Application()
    for path, handler in routes.items():
        app.router.add_get(path, handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    try:
        yield runner.addresses[0][1]
    finally:
        await runner.cleanup()


class Seen:
    """Every request a local server received: (path, X-Auth-* headers)."""

    def __init__(self) -> None:
        self.hits: List[tuple[str, Dict[str, str]]] = []

    def file(self, body: bytes) -> Handler:
        async def handler(request: web.Request) -> web.StreamResponse:
            self.hits.append((request.path, xauth(request)))
            return web.Response(body=body)

        return handler

    def redirect(self, status: int, location: str) -> Handler:
        async def handler(request: web.Request) -> web.StreamResponse:
            self.hits.append((request.path, xauth(request)))
            return web.Response(status=status, headers={"Location": location})

        return handler

    def status(self, status: int) -> Handler:
        async def handler(request: web.Request) -> web.StreamResponse:
            self.hits.append((request.path, xauth(request)))
            return web.Response(status=status)

        return handler


# ── the <Record/> element ────────────────────────────────────────────────


def test_record_element_overrides_every_vobiz_default_that_hurts_a_call():
    """https://www.vobiz.ai/docs/xml/record: playBeep defaults to true (the
    customer would hear a beep before the greeting), maxLength and the silence
    timeout default to 60 s (a call would be cut at a minute or at a pause).
    recordSession records the whole call; redirect="false" keeps the callback
    from taking over call control. Exactly these eight attributes."""
    xml = rec.vobiz_record_xml("CA-1")

    el = ET.fromstring(xml)
    assert el.tag == "Record"
    assert xml.startswith("<Record ") and xml.endswith("/>"), "must be self-closing"
    assert len(el) == 0 and not el.text
    assert el.attrib == {
        "recordSession": "true",
        "redirect": "false",
        "playBeep": "false",
        "fileFormat": "mp3",
        "maxLength": str(LIMIT),
        "timeout": str(LIMIT),
        "callbackUrl": f"{APP_BASE}{CALLBACK_PATH}?call_uuid=CA-1",
        "callbackMethod": "POST",
    }


def test_record_callback_url_carries_the_call_id_url_quoted():
    """A call id with '&', a space and '?' must round-trip through the
    callbackUrl's query string intact — RecordStop is matched to the lead by
    it — and must not leak a raw '&' into the XML."""
    call_id = "CA 1&x?y=z"
    xml = rec.vobiz_record_xml(call_id)

    callback = ET.fromstring(xml).attrib["callbackUrl"]
    assert callback == f"{APP_BASE}{CALLBACK_PATH}?call_uuid=CA%201%26x%3Fy%3Dz"
    assert parse_qs(urlsplit(callback).query) == {"call_uuid": [call_id]}


def test_record_callback_url_is_xml_escaped_as_a_whole(monkeypatch):
    """The whole callbackUrl value is XML-escaped, so a base URL carrying '&'
    or '"' still yields well-formed XML (a raw '&' makes Vobiz reject the
    answer, which drops the call)."""
    base = 'https://bb.example.test/x?a=1&b="2"'
    monkeypatch.setattr(rec, "APP_BASE_URL", base)

    xml = rec.vobiz_record_xml("CA-1")

    assert ET.fromstring(xml).attrib["callbackUrl"] == (
        f"{base}{CALLBACK_PATH}?call_uuid=CA-1"
    )


# ── the answer XML ───────────────────────────────────────────────────────


class FakeTemplate:
    def __init__(self) -> None:
        self.name = "support"
        self.merchant_id = "merchant-1"


@pytest.mark.parametrize(
    "result,template_id",
    [
        (
            {
                "is_outbound": True,
                "template_id": "tpl-out-1",
                "reseller_id": "res-1",
                "merchant_id": "merchant-1",
            },
            "tpl-out-1",
        ),
        (
            {
                "is_outbound": False,
                "reseller_id": "res-1",
                "template_list": [{"id": "tpl-in-1", "name": "support"}],
                "templates": [FakeTemplate()],
            },
            "tpl-in-1",
        ),
    ],
    ids=["outbound", "inbound"],
)
async def test_vobiz_answer_records_before_it_streams(monkeypatch, result, template_id):
    """https://www.vobiz.ai/docs/xml/record/stream-with-record: "Make <Record/>
    self-closing and place it before <Stream> as a sibling element."
    <Stream keepCallAlive> holds the call, so a <Record> after it never runs:
    the call would go unrecorded. Outbound and inbound answers share the
    builder; both are driven through _build_provider_response."""
    set_noise_cancellation(monkeypatch, True)

    async def no_pod(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(ans_mod, "safe_allocate_pod", no_pod)

    response = await ans_mod._build_provider_response(
        "vobiz", result, "CA-vz-1", "+918000000001", "+919000000001"
    )

    assert response.media_type == "application/xml"
    root = ET.fromstring(bytes(response.body))
    assert root.tag == "Response"
    assert [child.tag for child in root] == ["Record", "Stream"]
    record, stream = root
    assert record.attrib["recordSession"] == "true"
    assert (
        record.attrib["callbackUrl"] == f"{APP_BASE}{CALLBACK_PATH}?call_uuid=CA-vz-1"
    )
    assert stream.text == (
        "wss://bb.example.test/agent/voice/breeze-buddy/vobiz/callback/ws/v2"
        f"?template_id={template_id}&from_number=%2B918000000001"
        "&to_number=%2B919000000001"
    )


PLIVO_WS = (
    "wss://bb.example.test/agent/voice/breeze-buddy/plivo/callback/ws/v2"
    "?template_id=tpl-1&from_number=%2B918000000001&to_number=%2B919000000001"
)
# What release (f46d7449) rendered for _build_stream_xml(PLIVO_WS, "CA-plivo-1")
# (then named _build_plivo_stream_xml).
PLIVO_XML_NC_OFF = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n    <Record '
    'recordSession="true" recordChannelType="mono" maxLength="14400" '
    'callbackUrl="https://bb.example.test/agent/voice/breeze-buddy/plivo/callback'
    '/details?call_uuid=CA-plivo-1" callbackMethod="POST"/>\n    <Stream  '
    'bidirectional="true" keepCallAlive="true" '
    'contentType="audio/x-mulaw;rate=8000">\n        '
    "wss://bb.example.test/agent/voice/breeze-buddy/plivo/callback/ws/v2"
    "?template_id=tpl-1&amp;from_number=%2B918000000001&amp;to_number=%2B919000000001"
    "\n    </Stream>\n</Response>"
)
PLIVO_XML_NC_ON = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n    <Record '
    'recordSession="true" recordChannelType="mono" maxLength="14400" '
    'callbackUrl="https://bb.example.test/agent/voice/breeze-buddy/plivo/callback'
    '/details?call_uuid=CA-plivo-1" callbackMethod="POST"/>\n    <Stream '
    'noiseCancellation="true" noiseCancellationLevel="high" '
    'bidirectional="true" keepCallAlive="true" '
    'contentType="audio/x-mulaw;rate=8000">\n        '
    "wss://bb.example.test/agent/voice/breeze-buddy/plivo/callback/ws/v2"
    "?template_id=tpl-1&amp;from_number=%2B918000000001&amp;to_number=%2B919000000001"
    "\n    </Stream>\n</Response>"
)


@pytest.mark.parametrize(
    "enabled,expected",
    [(False, PLIVO_XML_NC_OFF), (True, PLIVO_XML_NC_ON)],
    ids=["noise-cancellation-off", "noise-cancellation-on"],
)
async def test_plivo_answer_xml_is_byte_identical_to_release(
    monkeypatch, enabled, expected
):
    """Vobiz's <Record> must not leak into Plivo's answer: every live Plivo
    call reads this XML."""
    set_noise_cancellation(monkeypatch, enabled)

    response = await ans_mod._build_xml_response(PLIVO_WS, "CA-plivo-1", "plivo")

    assert bytes(response.body) == expected.encode()


# ── who gets the credentials ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://api.vobiz.ai/api/v1/Account/MA/Recording/r1.mp3", CREDS),
        ("https://media.vobiz.ai/recordings/r1.mp3", CREDS),
        ("https://vobiz.ai/r1.mp3", CREDS),
        ("https://MEDIA.Vobiz.AI/r1.mp3", CREDS),
        # cleartext to a Vobiz host: the token would cross the wire in the clear
        ("http://api.vobiz.ai/api/v1/r1.mp3", {}),
        ("http://media.vobiz.ai/r1.mp3", {}),
        ("ftp://media.vobiz.ai/r1.mp3", {}),
        # look-alikes and other origins
        ("https://evil.com/r1.mp3", {}),
        ("https://vobiz.ai.evil.com/r1.mp3", {}),
        ("https://evilvobiz.ai/r1.mp3", {}),
        ("https://media.vobiz.ai@evil.com/r1.mp3", {}),
        ("https://evil.com/?next=https://media.vobiz.ai/r1.mp3", {}),
        ("https://storage.googleapis.com/bucket/r1.mp3", {}),
        # the configured API base, on its own host and scheme only
        (f"http://{API_HOST}:8791/media/r1.mp3", CREDS),
        (f"https://{API_HOST}:8791/media/r1.mp3", {}),
        (f"http://{OTHER_HOST}:8791/media/r1.mp3", {}),
        ("https:///r1.mp3", {}),
        ("not a url", {}),
        ("", {}),
    ],
)
def test_auth_headers_go_only_to_vobiz_over_https_or_the_api_host(url, expected):
    """RecordUrl arrives on an unauthenticated callback, so whoever can POST it
    chooses where we send the X-Auth headers
    (https://www.vobiz.ai/docs/api-reference/authentication). Only *.vobiz.ai
    over https, or the configured VOBIZ_API_BASE_URL host on its own scheme."""
    assert rec._vobiz_auth_headers(url) == expected


# ── the bytes decide the format ──────────────────────────────────────────


@pytest.mark.parametrize(
    "audio,expected",
    [
        (WAV, "wav"),
        (b"RIFF", "wav"),
        (ID3, "mp3"),
        (MPEG_FRAME, "mp3"),
        (b"", "mp3"),
        (b"RIF", "mp3"),
        (b"riff" + WAV[4:], "mp3"),
    ],
    ids=["wav", "bare-riff", "id3", "mpeg-frame", "empty", "truncated", "lowercase"],
)
def test_recording_format_comes_from_the_bytes(audio, expected):
    """Vobiz can serve WAV under an .mp3 URL; a WAV file starts with RIFF.
    Anything else (including nothing) is treated as mp3, the format we ask
    for."""
    assert rec.recording_file_format(audio) == expected


# ── the download ─────────────────────────────────────────────────────────


async def test_download_returns_the_bytes_and_sends_credentials_to_the_vobiz_host():
    seen = Seen()
    async with serve({"/rec.mp3": seen.file(WAV)}) as port:
        audio = await rec.download_call_recording(
            f"http://{API_HOST}:{port}/rec.mp3", "CA-1"
        )

    assert audio is not None and audio.getvalue() == WAV
    assert seen.hits == [("/rec.mp3", CREDS)]


async def test_download_from_another_host_sends_no_credentials():
    seen = Seen()
    async with serve({"/rec.mp3": seen.file(ID3)}) as port:
        audio = await rec.download_call_recording(
            f"http://{OTHER_HOST}:{port}/rec.mp3", "CA-1"
        )

    assert audio is not None and audio.getvalue() == ID3
    assert seen.hits == [("/rec.mp3", {})]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_a_cross_origin_redirect_hop_carries_no_credentials(status):
    """Redirects are followed by hand so each hop gets its own header decision:
    the Vobiz hop is authenticated, the other origin is not, and the file
    still arrives from the second hop."""
    vobiz, other = Seen(), Seen()
    async with serve({"/file.mp3": other.file(WAV)}) as other_port:
        target = f"http://{OTHER_HOST}:{other_port}/file.mp3"
        async with serve({"/rec.mp3": vobiz.redirect(status, target)}) as port:
            audio = await rec.download_call_recording(
                f"http://{API_HOST}:{port}/rec.mp3", "CA-1"
            )

    assert audio is not None and audio.getvalue() == WAV
    assert vobiz.hits == [("/rec.mp3", CREDS)]
    assert other.hits == [("/file.mp3", {})]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_a_same_host_relative_redirect_keeps_the_credentials(status):
    seen = Seen()
    routes = {"/rec.mp3": seen.redirect(status, "/real/rec.mp3")}
    routes["/real/rec.mp3"] = seen.file(ID3)
    async with serve(routes) as port:
        audio = await rec.download_call_recording(
            f"http://{API_HOST}:{port}/rec.mp3", "CA-1"
        )

    assert audio is not None and audio.getvalue() == ID3
    assert seen.hits == [("/rec.mp3", CREDS), ("/real/rec.mp3", CREDS)]


def _chain(seen: Seen) -> Dict[str, Handler]:
    """/hop/<n> redirects to /hop/<n-1>; /hop/0 is the file."""
    routes = {f"/hop/{n}": seen.redirect(302, f"/hop/{n - 1}") for n in range(1, 8)}
    routes["/hop/0"] = seen.file(WAV)
    return routes


async def test_five_redirects_are_followed():
    seen = Seen()
    async with serve(_chain(seen)) as port:
        audio = await rec.download_call_recording(
            f"http://{API_HOST}:{port}/hop/5", "CA-1"
        )

    assert audio is not None and audio.getvalue() == WAV
    assert len(seen.hits) == 6


async def test_a_sixth_redirect_gives_up_with_none():
    seen = Seen()
    async with serve(_chain(seen)) as port:
        audio = await rec.download_call_recording(
            f"http://{API_HOST}:{port}/hop/6", "CA-1"
        )

    assert audio is None
    assert len(seen.hits) == 6


async def test_a_redirect_loop_stops_after_five_hops_with_none():
    seen = Seen()
    async with serve({"/loop": seen.redirect(302, "/loop")}) as port:
        audio = await rec.download_call_recording(
            f"http://{API_HOST}:{port}/loop", "CA-1"
        )

    assert audio is None
    assert len(seen.hits) == 6


@pytest.mark.parametrize("status", [201, 204, 302, 401, 403, 404, 500, 503])
async def test_a_non_200_answer_gives_none(status):
    """302 here has no Location header, so it is just another non-200."""
    seen = Seen()
    async with serve({"/rec.mp3": seen.status(status)}) as port:
        audio = await rec.download_call_recording(
            f"http://{API_HOST}:{port}/rec.mp3", "CA-1"
        )

    assert audio is None
    assert len(seen.hits) == 1


async def test_an_unreachable_host_gives_none_not_an_exception():
    async with serve({}) as port:
        pass  # the server is gone: nothing listens on this port any more

    audio = await rec.download_call_recording(
        f"http://{API_HOST}:{port}/rec.mp3", "CA-1"
    )

    assert audio is None


def slow(seen: Seen, seconds: float, then: Handler) -> Handler:
    """Answer like ``then``, but only after ``seconds`` of silence."""

    async def handler(request: web.Request) -> web.StreamResponse:
        seen.hits.append((request.path, xauth(request)))
        await asyncio.sleep(seconds)
        return await then(request)

    return handler


async def test_a_silent_vobiz_host_gives_up_at_the_deadline(monkeypatch):
    """Console playback awaits this download: a host that never answers must
    not hold the request for aiohttp's 300 s default."""
    monkeypatch.setattr(rec, "_DOWNLOAD_TIMEOUT_SECONDS", 0.2)
    seen = Seen()
    async with serve({"/rec.mp3": slow(seen, 1.0, Seen().file(WAV))}) as port:
        started = time.monotonic()
        audio = await rec.download_call_recording(
            f"http://{API_HOST}:{port}/rec.mp3", "CA-1"
        )
        elapsed = time.monotonic() - started

    assert audio is None
    assert elapsed < 0.8
    assert len(seen.hits) == 1


async def test_the_deadline_covers_every_redirect_hop_together(monkeypatch):
    """Three hops of 0.25 s each fit a per-request 0.4 s timeout one by one,
    but not one 0.4 s deadline for the whole download."""
    monkeypatch.setattr(rec, "_DOWNLOAD_TIMEOUT_SECONDS", 0.4)
    seen, inner = Seen(), Seen()
    routes = {
        "/hop/2": slow(seen, 0.25, inner.redirect(302, "/hop/1")),
        "/hop/1": slow(seen, 0.25, inner.redirect(302, "/hop/0")),
        "/hop/0": slow(seen, 0.25, inner.file(WAV)),
    }
    async with serve(routes) as port:
        audio = await rec.download_call_recording(
            f"http://{API_HOST}:{port}/hop/2", "CA-1"
        )

    assert audio is None
    assert [path for path, _ in seen.hits][:2] == ["/hop/2", "/hop/1"]


# ── the RecordStop callback ──────────────────────────────────────────────


def make_callback(query: str, form: Dict[str, str]) -> Request:
    body = urlencode(form).encode()

    async def receive() -> Dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": CALLBACK_PATH,
        "query_string": query.encode(),
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
    }
    return Request(scope, receive)


class StoreSpy:
    def __init__(self) -> None:
        self.calls: List[tuple[str, str, str]] = []

    async def __call__(self, call_id: str, url: str, provider: str) -> None:
        self.calls.append((call_id, url, provider))


@pytest.mark.parametrize(
    "query,form,expected",
    [
        (
            "call_uuid=OUR-1",
            {"Event": "RecordStop", "RecordUrl": "https://media.vobiz.ai/r1.mp3"},
            [("OUR-1", "https://media.vobiz.ai/r1.mp3", "vobiz")],
        ),
        (
            "call_uuid=OUR-1",
            {"Event": "RecordStop", "RecordFile": "https://media.vobiz.ai/r1.mp3"},
            [("OUR-1", "https://media.vobiz.ai/r1.mp3", "vobiz")],
        ),
        (
            "call_uuid=OUR-1",
            {
                "RecordUrl": "https://media.vobiz.ai/url.mp3",
                "RecordFile": "https://media.vobiz.ai/file.mp3",
            },
            [("OUR-1", "https://media.vobiz.ai/url.mp3", "vobiz")],
        ),
        (
            "call_uuid=OUR-1",
            {"CallUUID": "THEIRS-2", "RecordUrl": "https://media.vobiz.ai/r1.mp3"},
            [("OUR-1", "https://media.vobiz.ai/r1.mp3", "vobiz")],
        ),
        (
            "call_uuid=CA%201%26x%3Fy",
            {"RecordUrl": "https://media.vobiz.ai/r1.mp3"},
            [("CA 1&x?y", "https://media.vobiz.ai/r1.mp3", "vobiz")],
        ),
        (
            "",
            {"CallUUID": "THEIRS-2", "RecordFile": "https://media.vobiz.ai/r1.mp3"},
            [("THEIRS-2", "https://media.vobiz.ai/r1.mp3", "vobiz")],
        ),
        ("call_uuid=OUR-1", {"Event": "RecordStop", "CallUUID": "THEIRS-2"}, []),
        ("call_uuid=OUR-1", {"RecordUrl": "", "RecordFile": ""}, []),
        ("", {"RecordUrl": "https://media.vobiz.ai/r1.mp3"}, []),
    ],
    ids=[
        "record-url",
        "record-file",
        "record-url-wins-over-record-file",
        "our-call-uuid-wins-over-the-form",
        "quoted-call-uuid-round-trips",
        "form-call-uuid-is-the-fallback",
        "no-file-url-writes-nothing",
        "empty-file-url-writes-nothing",
        "no-call-id-writes-nothing",
    ],
)
async def test_record_stop_callback_stores_the_recording(
    monkeypatch, query, form, expected
):
    """https://www.vobiz.ai/docs/xml/record: RecordStop sends the file as
    RecordFile or RecordUrl ("when returned under this field name"), so both
    must store. The call_uuid we put on callbackUrl names the lead; the form's
    CallUUID is only a fallback."""
    spy = StoreSpy()
    monkeypatch.setattr(cb_mod, "update_call_recording", spy)
    tasks = BackgroundTasks()

    response = await cb_mod.handle_callback_details_post(
        make_callback(query, form), "vobiz", tasks
    )
    await tasks()

    assert response.status_code == 200
    assert spy.calls == expected


# ── the GCS upload ───────────────────────────────────────────────────────


class Uploads:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.stored: List[tuple[str, str]] = []

    def upload(
        self,
        file_obj: BytesIO,
        destination_path: str,
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
    ) -> str:
        self.calls.append(
            {
                "bytes": file_obj.getvalue(),
                "path": destination_path,
                "content_type": content_type,
                "metadata": metadata,
            }
        )
        return f"https://gcs.example/{destination_path}"

    async def store(self, call_id: str, url: str) -> None:
        self.stored.append((call_id, url))


@pytest.mark.parametrize(
    "audio,extension,content_type",
    [
        (WAV, "wav", "audio/wav"),
        (ID3, "mp3", "audio/mpeg"),
        (MPEG_FRAME, "mp3", "audio/mpeg"),
    ],
    ids=["wav-under-mp3-url", "id3", "mpeg-frame"],
)
async def test_gcs_object_is_named_from_the_bytes(
    monkeypatch, audio, extension, content_type
):
    """The provider URL always ends in .mp3; a WAV body uploaded as .mp3 /
    audio/mpeg would not play in the console."""
    call_id = "CA-vz-1"
    url = "https://media.vobiz.ai/v1/Account/MA/Recording/rec-1.mp3"
    uploads = Uploads()
    downloads: List[tuple[str, str]] = []

    async def fake_lead(cid: str) -> LeadCallTracker:
        return LeadCallTracker(id="lead-1", reseller_id="res-1", template="t")

    async def fake_vobiz_download(u: str, cid: str) -> BytesIO:
        downloads.append((u, cid))
        return BytesIO(audio)

    async def other_download(u: str, cid: str) -> BytesIO:
        raise AssertionError("a vobiz recording went to another provider")

    monkeypatch.setattr(calls_mod, "UPLOAD_BREEZE_BUDDY_CALL_RECORDINGS_TO_CLOUD", True)
    monkeypatch.setattr(calls_mod, "get_lead_by_call_id", fake_lead)
    monkeypatch.setattr(calls_mod, "download_call_recording_vobiz", fake_vobiz_download)
    monkeypatch.setattr(calls_mod, "download_call_recording_plivo", other_download)
    monkeypatch.setattr(calls_mod, "upload_file_to_gcs", uploads.upload)
    monkeypatch.setattr(calls_mod, "update_lead_call_recording_url", uploads.store)

    await calls_mod.update_call_recording(call_id, url, "vobiz")

    destination = f"breeze-buddy/recordings/{call_id}.{extension}"
    assert downloads == [(url, call_id)]
    assert uploads.calls == [
        {
            "bytes": audio,
            "path": destination,
            "content_type": content_type,
            "metadata": {"call_id": call_id, "original_url": url},
        }
    ]
    assert uploads.stored == [(call_id, f"https://gcs.example/{destination}")]


# ── console playback ─────────────────────────────────────────────────────


async def test_console_plays_a_vobiz_recording_through_the_authenticated_download(
    monkeypatch,
):
    """The console's recording endpoint must fetch a VOBIZ lead's file with the
    Vobiz X-Auth headers (not Plivo's basic auth, not a 400)."""
    seen = Seen()
    async with serve({"/media/rec.mp3": seen.file(ID3)}) as port:
        lead = LeadCallTracker(
            id="lead-1",
            reseller_id="res-1",
            merchant_id="merchant-1",
            template="t",
            status=LeadCallStatus.FINISHED,
            call_id="CA-vz-1",
            execution_mode=ExecutionMode.TELEPHONY,
            telephony_number_id="num-1",
            recording_url=f"http://{API_HOST}:{port}/media/rec.mp3",
        )
        number = TelephonyNumber(
            id="num-1",
            number="+918000000001",
            provider=CallProvider.VOBIZ,
            status=TelephonyNumberStatus.AVAILABLE,
        )

        async def fake_lead(call_sid: str) -> LeadCallTracker:
            return lead

        async def fake_number(number_id: str) -> TelephonyNumber:
            return number

        monkeypatch.setattr(leads_mod, "get_lead_by_call_id", fake_lead)
        monkeypatch.setattr(leads_mod, "get_telephony_number_by_id", fake_number)
        user = UserInfo(id="u-1", username="admin@example.com", role=UserRole.ADMIN)

        result = await leads_mod.get_call_recording_handler("CA-vz-1", user)

    assert result.is_daily is False
    assert result.audio_file.read() == ID3
    assert seen.hits == [("/media/rec.mp3", CREDS)]
