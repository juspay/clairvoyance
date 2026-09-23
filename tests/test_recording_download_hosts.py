"""Where a recording may be fetched from, and where its keys may go.

Two different questions, and conflating them was the bug. The URL arrives in a
provider webhook nobody signed, so the provider's master credentials may only
go to that provider's own hosts. But the file itself may legitimately live
somewhere nobody wrote down — a regional CDN, or our own storage once
UPLOAD_BREEZE_BUDDY_CALL_RECORDINGS_TO_CLOUD has copied it and rewritten the
lead's recording_url. Gating REACHABILITY on the credential allow-list made
every one of those a silent failure, and playback answered 502.

So: the address check makes the fetch safe, and the allow-list only decides who
gets the keys. Plivo's per-account version of this lives in
tests/accounts/test_telephony_paths.py; these are the two providers that still
use platform-wide credentials.
"""

from __future__ import annotations

import pytest

import app.ai.voice.agents.breeze_buddy.services.telephony.exotel.recording as exotel
import app.ai.voice.agents.breeze_buddy.services.telephony.twilio.recording as twilio
from app.core.config.static import RECORDING_STORAGE_HOST
from app.core.network import fetch_bytes_from_allowed_host

STORED = f"https://{RECORDING_STORAGE_HOST}/breeze-buddy/recordings/call-1.mp3"

MODULES = pytest.mark.parametrize(
    "mod, provider_url",
    [
        (twilio, "https://api.twilio.com/2010-04-01/Recordings/RE1"),
        (exotel, "https://api.exotel.com/v1/Accounts/x/rec.mp3"),
    ],
    ids=["twilio", "exotel"],
)


@pytest.fixture
def fetched(monkeypatch):
    """Record what each module hands the guard, and answer with bytes."""
    calls: list[dict] = []

    async def _fake(url, *, allowed_host_suffixes=None, auth=None, **kw):
        calls.append({"url": url, "allowed": allowed_host_suffixes, "auth": auth})
        return b"audio"

    for mod in (twilio, exotel):
        monkeypatch.setattr(mod, "fetch_bytes_from_allowed_host", _fake)
    return calls


@MODULES
async def test_a_recording_anywhere_else_is_still_fetched(mod, provider_url, fetched):
    """The regression this closes: playback 502'd for every stored recording."""
    assert await mod.download_call_recording(STORED, "call-1") is not None

    assert fetched[-1]["allowed"] is None, "no allow-list when no keys are sent"


@MODULES
async def test_the_keys_do_not_follow_it_there(mod, provider_url, fetched):
    """Our storage has no use for a provider's master credentials, and sending
    them where they are not needed is how they end up where they should not be."""
    await mod.download_call_recording(STORED, "call-1")

    assert fetched[-1]["auth"] is None


@MODULES
async def test_a_forged_callback_host_gets_no_keys(mod, provider_url, fetched):
    """Anyone can POST to the callback that names this URL."""
    await mod.download_call_recording("https://attacker.example/steal.mp3", "call-1")

    assert fetched[-1]["auth"] is None


@MODULES
async def test_the_provider_still_gets_its_keys(mod, provider_url, fetched):
    assert await mod.download_call_recording(provider_url, "call-1") is not None

    assert fetched[-1]["auth"] is not None
    assert fetched[-1]["allowed"], "keys ride only with the list that bounds them"


async def test_keys_without_an_allow_list_are_refused_outright():
    """The two arguments are coupled, so the coupling is enforced rather than
    documented — an allow-list is what keeps credentials off a forged host."""
    with pytest.raises(ValueError, match="allow-list"):
        await fetch_bytes_from_allowed_host(
            "https://api.twilio.com/x",
            auth=object(),  # pyrefly: ignore[bad-argument-type]
        )


async def test_an_empty_allow_list_does_not_block_every_host(monkeypatch):
    """[] used to pass the credential check and then refuse every host at hop 0
    — a confusing way to spell "no list". It now means what it reads as."""
    import app.core.network.aiohttp_request as net
    import app.core.network.egress as egress

    async def _public(hostname, port):
        return ["93.184.216.34"]

    class _Resp:
        status = 200
        headers: dict = {}

        class content:
            _left = [b"audio"]

            @classmethod
            async def read(cls, _n):
                return cls._left.pop() if cls._left else b""

        def release(self):
            pass

    class _Session:
        timeout = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(egress, "_resolve_host", _public)
    monkeypatch.setattr(net, "create_aiohttp_session", lambda **kw: _Session())

    got = await fetch_bytes_from_allowed_host(
        "https://unlisted.example/rec.mp3", allowed_host_suffixes=[]
    )

    assert got == b"audio"
