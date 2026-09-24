"""A recording is fetched back from wherever it now lives.

The download functions are asked for two different kinds of URL. Usually it is
the provider's own, straight off an unsigned callback — which is why the host is
allow-listed before master credentials go anywhere near it. But when
UPLOAD_BREEZE_BUDDY_CALL_RECORDINGS_TO_CLOUD is on, the recording is copied to
our storage and the lead's recording_url is REWRITTEN to point there
(managers/calls.py), and the playback endpoint hands that same stored URL back
to the same provider function (leads/handlers.py). An allow-list naming only the
provider refuses hop 0 on every one of those, and playback answers 502.

So: both hosts are reachable, and the provider's credentials ride only to the
provider.
"""

from __future__ import annotations

import pytest

import app.ai.voice.agents.breeze_buddy.services.telephony.exotel.recording as exotel
import app.ai.voice.agents.breeze_buddy.services.telephony.plivo.recording as plivo
import app.ai.voice.agents.breeze_buddy.services.telephony.twilio.recording as twilio
from app.core.config.static import RECORDING_STORAGE_HOST

STORED = f"https://{RECORDING_STORAGE_HOST}/breeze-buddy/recordings/call-1.mp3"

MODULES = pytest.mark.parametrize(
    "mod, provider_url",
    [
        (twilio, "https://api.twilio.com/2010-04-01/Recordings/RE1"),
        (plivo, "https://media.plivo.com/v1/Account/MA/Recording/r1.mp3"),
        (exotel, "https://api.exotel.com/v1/Accounts/x/rec.mp3"),
    ],
    ids=["twilio", "plivo", "exotel"],
)


@pytest.fixture
def fetched(monkeypatch):
    """Record what each module hands the guard, and answer with bytes."""
    calls: list[dict] = []

    async def _fake(url, *, allowed_host_suffixes, auth=None, **kw):
        calls.append(
            {"url": url, "allowed": tuple(allowed_host_suffixes), "auth": auth}
        )
        return b"audio"

    for mod in (twilio, plivo, exotel):
        monkeypatch.setattr(mod, "fetch_bytes_from_allowed_host", _fake)
    return calls


@MODULES
async def test_a_stored_recording_is_reachable(mod, provider_url, fetched):
    """The regression: playback 502s for every recording when this is refused."""
    assert await mod.download_call_recording(STORED, "call-1") is not None
    assert RECORDING_STORAGE_HOST in fetched[-1]["allowed"]


@MODULES
async def test_the_providers_credentials_do_not_follow_it_there(
    mod, provider_url, fetched
):
    """Our storage has no use for a provider's master credentials, and sending
    them anywhere they are not needed is how they end up somewhere they are."""
    await mod.download_call_recording(STORED, "call-1")

    assert fetched[-1]["auth"] is None


@MODULES
async def test_the_provider_still_gets_its_credentials(mod, provider_url, fetched):
    assert await mod.download_call_recording(provider_url, "call-1") is not None

    assert fetched[-1]["auth"] is not None


@MODULES
async def test_a_forged_callback_host_is_still_refused(mod, provider_url, fetched):
    """Neither list may be widened into "anything"."""
    await mod.download_call_recording("https://attacker.example/steal.mp3", "call-1")

    allowed = fetched[-1]["allowed"]
    assert not any(
        "attacker.example".endswith(suffix) for suffix in allowed
    ), f"attacker.example matches {allowed}"
    # and it certainly does not get the credentials
    assert fetched[-1]["auth"] is None
