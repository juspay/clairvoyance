"""
Tests for where a call recording URL may point.

The URL arrives on an unauthenticated provider callback, is linked from the
console and is fetched by the server with the provider's keys. So only a URL
on the media host of the provider the lead's own number is on is stored; the
keys go only there; every redirect hop is checked so none reaches an internal
address; and the console's playback fetches only that host or our self-hosted
copy. The good URLs below use the hosts lead_call_tracker.recording_url has
held (as of October 2026).

Nothing here reaches a provider, GCS, Postgres or Redis: DNS and HTTP are
faked, everything else is monkeypatched.
"""

from __future__ import annotations

import os
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import aiohttp
import pytest
from fastapi import HTTPException

# dispatch must import first: managers.calls and dispatch.worker import each
# other, and only this order resolves it (the order the app itself loads them
# in). "...breeze_buddy" sorts before "...breeze_buddy.managers", so isort
# keeps this line above the next one on its own.
from app.ai.voice.agents.breeze_buddy import dispatch as _dispatch  # noqa: F401
from app.ai.voice.agents.breeze_buddy.accounts import PlivoAccount
from app.ai.voice.agents.breeze_buddy.managers import calls as calls_mod
from app.ai.voice.agents.breeze_buddy.services.telephony import recording_hosts
from app.ai.voice.agents.breeze_buddy.services.telephony.exotel import (
    recording as exotel_rec,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.plivo import (
    recording as plivo_rec,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.twilio import (
    recording as twilio_rec,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.vobiz import (
    recording as vobiz_rec,
)
from app.api.routers.breeze_buddy.leads import handlers as leads_mod
from app.core.config import static
from app.core.security import ssrf
from app.schemas import CallProvider, ExecutionMode, LeadCallStatus
from app.schemas.breeze_buddy.auth import UserInfo, UserRole
from app.schemas.breeze_buddy.core import (
    LeadCallTracker,
    TelephonyNumber,
    TelephonyNumberStatus,
)

REPO = Path(__file__).resolve().parents[2]

TWILIO_URL = "https://api.twilio.com/2010-04-01/Accounts/AC1/Recordings/RE1"
PLIVO_URL = "https://aps1.media.plivo.com/v1/Account/MA1/Recording/r1.mp3"
EXOTEL_URL = "https://recordings.mum1.exotel.com/exotelrecordings/acct/CA1.mp3"
VOBIZ_URL = "https://media.vobiz.ai/v1/Account/MA/Recording/rec-1.mp3"
SELF_HOSTED_URL = "https://sdk.beta.breezesdk.store/breeze-buddy/recordings/CA1.mp3"
METADATA_URL = "https://169.254.169.254/latest/meta-data/"
SCRIPT_URL = "javascript:alert(document.domain)"
# PlivoAccount checks the shape: 20 characters, MA... or SA...
PLIVO_AUTH_ID = "MATESTPLIVOACCOUNT01"

HOST_KEYS = [
    "TWILIO_RECORDING_HOSTS",
    "PLIVO_RECORDING_HOSTS",
    "EXOTEL_RECORDING_HOSTS",
    "VOBIZ_RECORDING_HOSTS",
    "SELF_HOSTED_RECORDING_HOSTS",
]
DEFAULT_HOSTS = [
    ["api.twilio.com"],
    ["media.plivo.com"],
    ["recordings.exotel.com", "recordings.mum1.exotel.com"],
    ["media.vobiz.ai"],
    ["sdk.beta.breezesdk.store"],
]


@pytest.fixture(autouse=True)
def default_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The checks read the host keys at call time: pin them to the defaults,
    so an environment that sets one does not change what these tests check."""
    for key, hosts in zip(HOST_KEYS, DEFAULT_HOSTS):
        monkeypatch.setattr(recording_hosts, key, hosts)


# ── the config keys ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, ["media.plivo.com"]),
        # set but empty (an empty chart secret): the default, not "refuse all"
        ("", ["media.plivo.com"]),
        (" , ", ["media.plivo.com"]),
        ("media.plivo.com,cdn.plivo.com", ["media.plivo.com", "cdn.plivo.com"]),
        (" Media.Plivo.COM , .cdn.plivo.com ", ["media.plivo.com", "cdn.plivo.com"]),
    ],
)
def test_a_hosts_key_is_comma_separated_with_a_default(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("PLIVO_RECORDING_HOSTS", raising=False)
    else:
        monkeypatch.setenv("PLIVO_RECORDING_HOSTS", value)

    hosts = static._recording_hosts("PLIVO_RECORDING_HOSTS", "media.plivo.com")

    assert hosts == expected


@pytest.mark.parametrize(
    "value",
    [
        # suffix match: these would trust every .com host with the keys
        "com",
        ".com",
        "media.plivo.com,com",
        # not a host name
        "https://media.plivo.com",
        "media.plivo.com/recordings",
        "media.plivo.com:443",
        "*.plivo.com",
        ".",
    ],
)
def test_a_hosts_key_that_is_not_a_host_name_fails_at_boot(monkeypatch, value):
    monkeypatch.setenv("PLIVO_RECORDING_HOSTS", value)

    with pytest.raises(ValueError, match="PLIVO_RECORDING_HOSTS"):
        static._recording_hosts("PLIVO_RECORDING_HOSTS", "media.plivo.com")


def test_the_defaults_are_the_media_hosts_recordings_are_on():
    """With no key set, each provider trusts only the media hosts its
    recordings have been on, never its whole domain. Booted in a fresh
    interpreter: static config is read once at import, and an environment that
    sets a key must not change what this checks."""
    env = {k: v for k, v in os.environ.items() if k not in HOST_KEYS}
    names = ", ".join(f"s.{key}" for key in HOST_KEYS)
    booted = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import app.core.config.static as s; print([{names}])",
        ],
        env={**env, "PYTHONPATH": str(REPO)},
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert booted.returncode == 0, booted.stderr
    assert booted.stdout.strip() == str(DEFAULT_HOSTS)


# ── which hosts count ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "provider,url",
    [
        ("twilio", TWILIO_URL),
        ("plivo", PLIVO_URL),
        ("plivo", "https://media.plivo.com/v1/Account/MA1/Recording/r1.mp3"),
        ("vobiz", VOBIZ_URL),
        ("exotel", "https://recordings.exotel.com/exotelrecordings/acct/CA1.mp3"),
        ("exotel", EXOTEL_URL),
        # the console passes the CallProvider value, upper case
        ("TWILIO", TWILIO_URL),
    ],
)
def test_a_recording_on_the_providers_own_host_is_accepted(provider, url):
    assert recording_hosts.is_provider_recording_url(provider, url)


@pytest.mark.parametrize(
    "provider,url",
    [
        # what the console would run or render
        ("twilio", SCRIPT_URL),
        ("twilio", "data:text/html,<script>alert(1)</script>"),
        # cleartext: the keys would cross the wire in the clear
        ("twilio", "http://api.twilio.com/2010-04-01/Accounts/AC1/Recordings/RE1"),
        # look-alikes
        ("twilio", "https://api.twilio.com.evil.com/r.wav"),
        ("twilio", "https://api.twilio.com@evil.com/r.wav"),
        ("exotel", "https://recordings.exotel.com.evil.com/r.mp3"),
        ("exotel", "https://evil.com/?next=https://recordings.exotel.com/r.mp3"),
        # the provider's other sites: a help desk or docs host is not media
        ("exotel", "https://support.exotel.com/r.mp3"),
        ("exotel", "https://exotel.com/r.mp3"),
        ("vobiz", "https://docs.vobiz.ai/r1.mp3"),
        ("vobiz", "https://vobiz.ai/r1.mp3"),
        # a browser reads this host as evil.com, urlsplit as media.vobiz.ai
        ("vobiz", "https://evil.com\\@media.vobiz.ai/r1.mp3"),
        ("twilio", "https://api.twilio.com/r.wav\nhttps://evil.com"),
        # another provider's host, or Plivo's API rather than its media
        ("plivo", VOBIZ_URL),
        ("plivo", "https://api.plivo.com/v1/Account/x/Call/"),
        # any bucket can sit on S3; no Exotel recording we hold is there
        ("exotel", "https://s3-ap-southeast-1.amazonaws.com/exotelrecordings/r.mp3"),
        # our self-hosted copy is not a provider recording
        ("twilio", SELF_HOSTED_URL),
        ("twilio", METADATA_URL),
        ("daily", TWILIO_URL),
        ("twilio", ""),
        ("twilio", "https://[::1"),
    ],
)
def test_anything_else_is_refused(provider, url):
    assert not recording_hosts.is_provider_recording_url(provider, url)


def test_vobiz_also_trusts_its_configured_api_host(monkeypatch):
    """VOBIZ_API_BASE_URL's host counts on its own scheme (in production
    https://api.vobiz.ai; the local harness points it at an http stand-in)."""
    monkeypatch.setattr(recording_hosts, "_VOBIZ_API_HOST", "api.vobiz.ai")
    monkeypatch.setattr(recording_hosts, "_VOBIZ_API_SCHEME", "https")
    url = "https://api.vobiz.ai/api/v1/Account/MA/Recording/r1.mp3"

    assert recording_hosts.is_provider_recording_url("vobiz", url)
    assert not recording_hosts.is_provider_recording_url("vobiz", "http" + url[5:])


@pytest.mark.parametrize(
    "url,expected",
    [
        (SELF_HOSTED_URL, True),
        ("http://sdk.beta.breezesdk.store/breeze-buddy/recordings/CA1.mp3", False),
        ("https://sdk.beta.breezesdk.store.evil.com/r.mp3", False),
        ("https://evil.com/sdk.beta.breezesdk.store/r.mp3", False),
        (SCRIPT_URL, False),
    ],
)
def test_a_self_hosted_recording_is_only_on_our_host(url, expected):
    assert recording_hosts.is_self_hosted_recording_url(url) is expected


# ── storing a callback's URL ─────────────────────────────────────────────


class Spy:
    def __init__(self) -> None:
        self.calls: List[Tuple[Any, ...]] = []

    async def __call__(self, *args: Any) -> None:
        self.calls.append(args)


def callback_lead(
    monkeypatch: pytest.MonkeyPatch,
    provider: Optional[CallProvider],
    upload_to_cloud: bool = False,
) -> Tuple[Spy, Spy]:
    """The lead a recording callback names, on a number with ``provider``
    (None: no number); returns the (store, download) spies."""
    stores, downloads = Spy(), Spy()
    lead = LeadCallTracker(
        id="lead-1",
        reseller_id="res-1",
        merchant_id="merchant-1",
        template="t",
        telephony_number_id="num-1" if provider else None,
    )

    async def fake_lead(call_id: str) -> LeadCallTracker:
        return lead

    async def fake_number(number_id: str) -> Optional[TelephonyNumber]:
        assert provider is not None, "a lead with no number was looked up"
        return TelephonyNumber(
            id=number_id,
            number="+918000000001",
            provider=provider,
            status=TelephonyNumberStatus.AVAILABLE,
        )

    monkeypatch.setattr(
        calls_mod, "UPLOAD_BREEZE_BUDDY_CALL_RECORDINGS_TO_CLOUD", upload_to_cloud
    )
    monkeypatch.setattr(calls_mod, "get_lead_by_call_id", fake_lead)
    monkeypatch.setattr(calls_mod, "get_telephony_number_by_id", fake_number)
    monkeypatch.setattr(calls_mod, "update_lead_call_recording_url", stores)
    for name in ("twilio", "exotel", "plivo", "vobiz"):
        monkeypatch.setattr(calls_mod, f"download_call_recording_{name}", downloads)
    return stores, downloads


@pytest.mark.parametrize("upload_to_cloud", [False, True], ids=["store", "upload"])
@pytest.mark.parametrize(
    "provider,url",
    [
        ("twilio", SCRIPT_URL),
        ("twilio", "https://attacker.example/r.wav"),
        ("exotel", METADATA_URL),
        ("exotel", "https://support.exotel.com/r.mp3"),
    ],
)
async def test_a_bad_callback_url_is_neither_stored_nor_fetched(
    monkeypatch, upload_to_cloud, provider, url
):
    """Before this check, a failed download stored the callback's URL as is."""
    stores, downloads = callback_lead(
        monkeypatch, CallProvider(provider.upper()), upload_to_cloud
    )

    await calls_mod.update_call_recording("CA-1", url, provider)

    assert stores.calls == [] and downloads.calls == []


@pytest.mark.parametrize("upload_to_cloud", [False, True], ids=["store", "upload"])
async def test_another_providers_recording_is_not_swapped_onto_a_call(
    monkeypatch, upload_to_cloud
):
    """The callback path names its own provider: a real Plivo recording posted
    to /plivo/callback/details for a Vobiz lead's call passes a "plivo" host
    check, but the lead's number is on Vobiz."""
    stores, downloads = callback_lead(monkeypatch, CallProvider.VOBIZ, upload_to_cloud)

    await calls_mod.update_call_recording("CA-1", PLIVO_URL, "plivo")

    assert stores.calls == [] and downloads.calls == []


async def test_a_lead_with_no_number_stores_no_recording(monkeypatch):
    """Every telephony lead a callback can name has a number (outbound sets it
    with call_id, inbound is created with it), and playback cannot serve one
    without it either."""
    stores, downloads = callback_lead(monkeypatch, None)

    await calls_mod.update_call_recording("CA-1", TWILIO_URL, "twilio")

    assert stores.calls == [] and downloads.calls == []


async def test_a_good_callback_url_is_still_stored(monkeypatch):
    stores, _ = callback_lead(monkeypatch, CallProvider.TWILIO)

    await calls_mod.update_call_recording("CA-1", TWILIO_URL, "twilio")

    assert stores.calls == [("CA-1", TWILIO_URL)]


# ── the download: who gets the keys, and every hop checked ───────────────


class FakeResponse:
    def __init__(self, status: int = 200, location: Optional[str] = None) -> None:
        self.status = status
        self.headers = {"Location": location} if location else {}

    async def read(self) -> bytes:
        return b"audio"

    def release(self) -> None:
        return None


class FakeNet:
    """DNS and aiohttp.ClientSession, faked. Every name resolves to a public
    address unless listed in ``addresses``; ``routes`` maps a URL to its
    response (200 b"audio" by default). Each request is recorded as the URL
    asked for (ssrf_safe_request connects to the checked address and carries
    the name in the Host header), its auth and its headers."""

    def __init__(self) -> None:
        self.addresses: Dict[str, str] = {}
        self.routes: Dict[str, FakeResponse] = {}
        self.requests: List[Tuple[str, Any, Dict[str, str]]] = []

    async def resolve(self, hostname: str, port: int) -> List[str]:
        return [self.addresses.get(hostname, "8.8.8.8")]

    def session(self) -> type:
        net = self

        class Session:
            async def request(
                self,
                method: str,
                url: str,
                *,
                auth: Any = None,
                headers: Optional[Dict[str, str]] = None,
                **kwargs: Any,
            ) -> FakeResponse:
                sent = dict(headers or {})
                host = sent.pop("Host", None)
                if host:
                    url = urlsplit(url)._replace(netloc=host).geturl()
                net.requests.append((url, auth, sent))
                return net.routes.get(url, FakeResponse())

            async def __aenter__(self) -> "Session":
                return self

            async def __aexit__(self, *_a: Any) -> None:
                return None

        return Session


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> FakeNet:
    fake = FakeNet()
    monkeypatch.setattr(ssrf, "_resolve_host", fake.resolve)
    # A local .env may allow private egress for development; these tests are
    # about production, where it is off.
    monkeypatch.setattr(ssrf, "_ALLOW_PRIVATE_EGRESS", False)
    monkeypatch.setattr(aiohttp, "ClientSession", fake.session())
    for mod in (twilio_rec, exotel_rec, plivo_rec, vobiz_rec):
        monkeypatch.setattr(mod, "get_proxy_config", lambda: None)
    monkeypatch.setattr(twilio_rec, "TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setattr(exotel_rec, "EXOTEL_API_KEY", "exotel-test")
    monkeypatch.setattr(vobiz_rec, "VOBIZ_AUTH_ID", "vobiz-test")

    async def plivo_lead(call_sid: str) -> LeadCallTracker:
        return LeadCallTracker(id="lead-1", reseller_id="res-1", template="t")

    async def plivo_account(lead: Any) -> PlivoAccount:
        return PlivoAccount(auth_id=PLIVO_AUTH_ID, auth_token="t")

    monkeypatch.setattr(plivo_rec, "get_lead_by_call_id", plivo_lead)
    monkeypatch.setattr(plivo_rec, "lead_plivo_account", plivo_account)
    return fake


def keys_sent(auth: Any, headers: Dict[str, str]) -> Optional[str]:
    """Which login a request carried: Basic auth, or Vobiz's X-Auth-ID."""
    return auth.login if auth else headers.get("X-Auth-ID")


DOWNLOADERS = {
    "twilio": (twilio_rec, TWILIO_URL, "AC-test"),
    "exotel": (exotel_rec, EXOTEL_URL, "exotel-test"),
    "plivo": (plivo_rec, PLIVO_URL, PLIVO_AUTH_ID),
    "vobiz": (vobiz_rec, VOBIZ_URL, "vobiz-test"),
}


@pytest.mark.parametrize("provider", list(DOWNLOADERS))
@pytest.mark.parametrize("where", ["own-host", "self-hosted", "other"])
async def test_the_keys_go_only_to_the_providers_own_host(net, provider, where):
    mod, provider_url, login = DOWNLOADERS[provider]
    url = {
        "own-host": provider_url,
        "self-hosted": SELF_HOSTED_URL,
        "other": "https://attacker.example/r.mp3",
    }[where]

    audio = await mod.download_call_recording(url, "CA-1")

    assert audio is not None and audio.read() == b"audio"
    [(fetched, auth, headers)] = net.requests
    assert fetched == url
    assert keys_sent(auth, headers) == (login if where == "own-host" else None)


@pytest.mark.parametrize("provider", list(DOWNLOADERS))
@pytest.mark.parametrize(
    "target",
    ["https://169.254.169.254/latest/meta-data/", "https://internal.example/x"],
    ids=["metadata-ip", "name-resolving-to-private"],
)
async def test_a_redirect_to_an_internal_address_is_refused(net, provider, target):
    """The host check covers only the first URL: a redirect from an allowed
    host (an open redirect on it, say) must not reach an internal address."""
    mod, url, _ = DOWNLOADERS[provider]
    net.addresses["internal.example"] = "10.0.0.7"
    net.routes[url] = FakeResponse(302, location=target)

    audio = await mod.download_call_recording(url, "CA-1")

    assert audio is None
    assert [fetched for fetched, _, _ in net.requests] == [url]


@pytest.mark.parametrize("provider", list(DOWNLOADERS))
async def test_a_redirect_off_the_providers_origin_drops_the_keys(net, provider):
    """Twilio serves media through a signed URL on another host: still fetched,
    without the keys."""
    mod, url, login = DOWNLOADERS[provider]
    signed = "https://signed-media.example/r?sig=x"
    net.routes[url] = FakeResponse(302, location=signed)

    audio = await mod.download_call_recording(url, "CA-1")

    assert audio is not None and audio.read() == b"audio"
    [(first, auth1, headers1), (second, auth2, headers2)] = net.requests
    assert (first, keys_sent(auth1, headers1)) == (url, login)
    assert (second, keys_sent(auth2, headers2)) == (signed, None)


async def test_an_http_url_is_not_fetched(net):
    """Cleartext only for Vobiz's configured API host (the local harness)."""
    audio = await twilio_rec.download_call_recording(
        "http://attacker.example/r.wav", "CA-1"
    )

    assert audio is None and net.requests == []


# ── console playback ─────────────────────────────────────────────────────


def playback_lead(
    monkeypatch: pytest.MonkeyPatch, recording_url: str, provider: CallProvider
) -> Spy:
    """A finished telephony lead on ``provider`` holding ``recording_url``;
    returns the spy every provider's download is replaced with."""
    lead = LeadCallTracker(
        id="lead-1",
        reseller_id="res-1",
        merchant_id="merchant-1",
        template="t",
        status=LeadCallStatus.FINISHED,
        call_id="CA-1",
        execution_mode=ExecutionMode.TELEPHONY,
        telephony_number_id="num-1",
        recording_url=recording_url,
    )
    number = TelephonyNumber(
        id="num-1",
        number="+918000000001",
        provider=provider,
        status=TelephonyNumberStatus.AVAILABLE,
    )
    downloads = Spy()

    async def fake_lead(call_sid: str) -> LeadCallTracker:
        return lead

    async def fake_number(number_id: str) -> TelephonyNumber:
        return number

    async def fake_download(url: str, call_sid: str) -> BytesIO:
        await downloads(url, call_sid)
        return BytesIO(b"audio")

    monkeypatch.setattr(leads_mod, "get_lead_by_call_id", fake_lead)
    monkeypatch.setattr(leads_mod, "get_telephony_number_by_id", fake_number)
    for name in ("twilio", "exotel", "plivo", "vobiz"):
        monkeypatch.setattr(leads_mod, f"download_call_recording_{name}", fake_download)
    return downloads


ADMIN = UserInfo(id="u-1", username="admin@example.com", role=UserRole.ADMIN)


@pytest.mark.parametrize(
    "url,provider",
    [
        (METADATA_URL, CallProvider.TWILIO),
        (SCRIPT_URL, CallProvider.EXOTEL),
        ("https://attacker.example/r.mp3", CallProvider.PLIVO),
        # a real Plivo recording planted on a Vobiz lead
        (PLIVO_URL, CallProvider.VOBIZ),
    ],
)
async def test_playback_refuses_a_stored_url_off_the_providers_host(
    monkeypatch, url, provider
):
    downloads = playback_lead(monkeypatch, url, provider)

    with pytest.raises(HTTPException) as refused:
        await leads_mod.get_call_recording_handler("CA-1", ADMIN)

    assert refused.value.status_code == 404
    assert downloads.calls == []


@pytest.mark.parametrize(
    "url,provider",
    [
        (TWILIO_URL, CallProvider.TWILIO),
        (EXOTEL_URL, CallProvider.EXOTEL),
        (SELF_HOSTED_URL, CallProvider.TWILIO),
    ],
    ids=["twilio", "exotel", "self-hosted"],
)
async def test_playback_fetches_the_providers_host_or_our_copy(
    monkeypatch, url, provider
):
    downloads = playback_lead(monkeypatch, url, provider)

    result = await leads_mod.get_call_recording_handler("CA-1", ADMIN)

    assert result.audio_file.read() == b"audio"
    assert downloads.calls == [(url, "CA-1")]
