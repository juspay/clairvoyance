"""
A recording URL arrives on an unsigned provider callback. update_call_recording,
the one writer of lead.recording_url, refuses a URL off the provider's recording
hosts (or our own uploaded copy) before it reads the lead, stores or downloads
anything, so a forged callback cannot make another host's file the call's
recording. Exotel is not pinned yet (it also hands back presigned object-store
URLs): any URL is kept, fetched without keys.
"""

from io import BytesIO
from typing import Any, List

import pytest

# dispatch must import first: managers.calls and dispatch.worker import each
# other, and only this order resolves it (the order the app itself loads them in).
from app.ai.voice.agents.breeze_buddy import dispatch as _dispatch  # noqa: F401
from app.ai.voice.agents.breeze_buddy.managers import calls as calls_mod
from app.schemas.breeze_buddy.core import LeadCallTracker

OUR_COPY = "https://sdk.beta.breezesdk.store/breeze-buddy/recordings/CA-1.mp3"
FORGED = [
    "https://evil.example/r.mp3",
    "https://api.twilio.com.evil.example/r.mp3",
    "https://media.plivo.com.evil.example/r.mp3",
    "https://media.vobiz.ai.evil.example/r.mp3",
    "https://media.vobiz.ai@evil.example/r.mp3",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5/admin",
    "not a url",
    "",
]


class Saves:
    def __init__(self) -> None:
        self.leads_read: List[str] = []
        self.saved: List[tuple[str, str]] = []

    async def lead(self, call_id: str) -> LeadCallTracker:
        self.leads_read.append(call_id)
        return LeadCallTracker(id="lead-1", reseller_id="r-1", template="t")

    async def save(self, call_id: str, url: str) -> None:
        self.saved.append((call_id, url))


@pytest.fixture
def saves(monkeypatch: pytest.MonkeyPatch) -> Saves:
    spy = Saves()
    monkeypatch.setattr(calls_mod, "get_lead_by_call_id", spy.lead)
    monkeypatch.setattr(calls_mod, "update_lead_call_recording_url", spy.save)
    return spy


@pytest.mark.parametrize(
    "provider, url",
    [
        ("twilio", "https://api.twilio.com/2010-04-01/Accounts/AC1/Recordings/RE1"),
        ("plivo", "https://media.plivo.com/v1/Account/MA1/Recording/r1.mp3"),
        ("plivo", "https://aps1.media.plivo.com/v1/Account/MA1/Recording/r1.mp3"),
        ("vobiz", "https://media.vobiz.ai/v1/Account/MA1/Recording/r1.mp3"),
        ("vobiz", "https://api.vobiz.ai/api/v1/Account/MA1/Recording/r1.mp3"),
        ("Twilio", "https://api.twilio.com/2010-04-01/Accounts/AC1/Recordings/RE1"),
        ("plivo", OUR_COPY),
        # Exotel: not pinned, so any URL is kept
        (
            "exotel",
            "https://s3-ap-southeast-1.amazonaws.com/exotelrecordings/a/CA-1.mp3",
        ),
        ("exotel", "https://evil.example/r.mp3"),
    ],
)
async def test_a_url_on_the_providers_hosts_is_stored(
    monkeypatch: pytest.MonkeyPatch, saves: Saves, provider: str, url: str
) -> None:
    monkeypatch.setattr(
        calls_mod, "UPLOAD_BREEZE_BUDDY_CALL_RECORDINGS_TO_CLOUD", False
    )

    await calls_mod.update_call_recording("CA-1", url, provider)

    assert saves.saved == [("CA-1", url)]


@pytest.mark.parametrize("url", FORGED)
@pytest.mark.parametrize("provider", ["twilio", "plivo", "vobiz", "daily", "unknown"])
async def test_a_url_off_the_providers_hosts_is_never_stored(
    monkeypatch: pytest.MonkeyPatch, saves: Saves, provider: str, url: str
) -> None:
    """Also for a provider with no recording hosts at all: refused, not stored."""
    monkeypatch.setattr(
        calls_mod, "UPLOAD_BREEZE_BUDDY_CALL_RECORDINGS_TO_CLOUD", False
    )

    await calls_mod.update_call_recording("CA-1", url, provider)

    assert saves.saved == []
    assert saves.leads_read == []


@pytest.mark.parametrize("provider", ["twilio", "plivo", "vobiz"])
async def test_a_url_off_the_providers_hosts_is_never_downloaded(
    monkeypatch: pytest.MonkeyPatch, saves: Saves, provider: str
) -> None:
    """With cloud upload on, the refusal comes before the download too: the
    provider URL is not even kept as the fallback."""
    downloads: List[Any] = []

    async def download(url: str, call_id: str) -> BytesIO:
        downloads.append(url)
        return BytesIO(b"ID3")

    monkeypatch.setattr(calls_mod, "UPLOAD_BREEZE_BUDDY_CALL_RECORDINGS_TO_CLOUD", True)
    for name in ("twilio", "exotel", "plivo", "vobiz"):
        monkeypatch.setattr(calls_mod, f"download_call_recording_{name}", download)

    await calls_mod.update_call_recording(
        "CA-1", "https://evil.example/r.mp3", provider
    )

    assert downloads == []
    assert saves.saved == []
