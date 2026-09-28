"""/tts/stream MISS at a rate other than the provider's native rate.

The cache can't resample chunk by chunk, so such a miss synthesizes the whole
clip first. Native-store models keep NATIVE bytes in the cache (hits convert),
and the miss itself must convert too: it used to serve the stored native bytes
as-is, so a 16 kHz caller got 8 kHz eleven_v3 audio (2x speed) on every miss
while every later hit was right. The contract pinned here: a miss serves
exactly what the following hit serves.
"""

from __future__ import annotations

import pytest

from app.cache.service import CacheService
from app.core.config import settings
from app.providers.base import AudioResult, BaseTTSProvider
from app.schemas.tts import CartesiaVoice, OutputFormat, TTSRequest
from app.storage.filesystem import FilesystemBlobStore
from app.storage.sqlite import SQLiteMetadataStore


class FixedRateProvider(BaseTTSProvider):
    """Synthesizes a fixed 0.5 s clip at ``rate`` (native)."""

    name = "cartesia"

    def __init__(self, rate: int) -> None:
        self.native_sample_rate = rate
        self.calls = 0

    async def synth(self, *, text, voice_id, model, language, params) -> AudioResult:
        self.calls += 1
        n = self.native_sample_rate // 2
        audio = b"".join(
            int(8000 * ((i % 40) / 20 - 1)).to_bytes(2, "little", signed=True)
            for i in range(n)
        )
        return AudioResult(
            audio=audio,
            container="raw",
            encoding="pcm_s16le",
            sample_rate=self.native_sample_rate,
        )


@pytest.mark.parametrize("native, requested", [(16000, 8000), (8000, 16000)])
async def test_stream_miss_serves_the_requested_rate_like_the_hit(
    native, requested, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "metrics_write_behind_enabled", False)
    meta = SQLiteMetadataStore(str(tmp_path / "cache.db"))
    await meta.init()
    blobs = FilesystemBlobStore(str(tmp_path / "blobs"))
    await blobs.init()
    provider = FixedRateProvider(native)
    svc = CacheService(meta, blobs, lambda n: provider if n == "cartesia" else None)

    def req() -> TTSRequest:
        return TTSRequest(
            model_id="cartesia:sonic-2",
            transcript="hello there",
            voice=CartesiaVoice(id="v"),
            language="en",
            output_format=OutputFormat(encoding="pcm_s16le", sample_rate=requested),
        )

    headers, body = await svc.stream(req())
    assert headers["X-Cache"] == "MISS"
    miss = b"".join([chunk async for chunk in body])
    headers, body = await svc.stream(req())
    assert headers["X-Cache"] == "HIT"
    hit = b"".join([chunk async for chunk in body])

    assert provider.calls == 1
    # 0.5 s at the REQUESTED rate, s16le mono — on the miss as on the hit.
    assert abs(len(miss) - requested) <= 4, (len(miss), requested)
    assert miss == hit
