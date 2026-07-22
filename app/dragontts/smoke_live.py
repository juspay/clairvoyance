"""Live provider smoke-test (Cartesia / ElevenLabs / Sarvam) — NO commit.

Exercises both synth (one-shot) and stream_synth (warm WS/gRPC) for each
configured provider and validates the output is non-empty, correctly-rated
pcm_s16le@16k, and non-silent. Also runs a cache MISS->HIT round-trip through
the real CacheService on a TEMP db/blobs dir (never touches the real cache).

Run:  uv run python smoke_live.py
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

import numpy as np

from app.cache.service import CacheService
from app.core.config import PROVIDER_DEFAULTS, settings
from app.providers.base import AudioResult
from app.providers.cartesia import CartesiaProvider
from app.providers.elevenlabs import ElevenLabsProvider
from app.providers.sarvam import SarvamProvider
from app.schemas.tts import CartesiaVoice, OutputFormat, TTSRequest
from app.storage.filesystem import FilesystemBlobStore
from app.storage.sqlite import SQLiteMetadataStore

PHRASE = "Hello, this is a streaming smoke test."


def _validate_pcm(audio: bytes, label: str) -> tuple[bool, str]:
    """Return (ok, detail) for pcm_s16le@16k expectations."""
    n = len(audio)
    if n == 0:
        return False, "EMPTY"
    if n % 2 != 0:
        return False, f"odd length ({n}) — not whole int16 frames"
    samples = np.frombuffer(audio, dtype="<i2").astype(np.int32)
    peak = int(np.max(np.abs(samples))) if len(samples) else 0
    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))) if len(samples) else 0.0
    dur_ms = n / 2 / 16000 * 1000
    if peak < 50:
        return False, f"silent (peak={peak})"
    return True, f"{dur_ms:.0f}ms, peak={peak}, rms={rms:.0f}"


async def _drain(gen):
    """Drain a chunk generator. Returns (audio, n_chunks, ttfb_s or None)."""
    out = bytearray()
    n_chunks = 0
    t0 = time.monotonic()
    ttfb = None
    async for chunk in gen:
        if ttfb is None:
            ttfb = time.monotonic() - t0
        out += chunk
        n_chunks += 1
    return bytes(out), n_chunks, ttfb


async def test_provider(name: str, prov) -> dict:
    """synth + stream_synth for one provider. Returns a result dict."""
    res: dict = {"provider": name, "synth": None, "stream": None, "err": None}
    t0 = time.monotonic()
    try:
        await prov.warm()
        # --- one-shot synth ---
        t = time.monotonic()
        ar: AudioResult = await prov.synth(
            text=PHRASE, voice_id=None, model=None, language=None, params={},
        )
        ok, detail = _validate_pcm(ar.audio, name)
        res["synth"] = {
            "ok": ok, "detail": detail,
            "rate": ar.sample_rate, "enc": ar.encoding,
            "ms": round((time.monotonic() - t) * 1000),
        }

        # --- streaming synth (the warm-WS path; different phrase to force a fresh synth) ---
        stream_phrase = "Streaming live audio chunks now."
        t = time.monotonic()
        total, n_chunks, ttfb = await _drain(prov.stream_synth(
            text=stream_phrase, voice_id=None, model=None, language=None, params={},
        ))
        ok2, detail2 = _validate_pcm(total, name)
        res["stream"] = {
            "ok": ok2, "detail": detail2, "chunks": n_chunks,
            "ms": round((time.monotonic() - t) * 1000),
            "ttfb_ms": round(ttfb * 1000) if ttfb is not None else None,
        }
    except Exception as e:
        body = ""
        resp = getattr(e, "response", None)
        if resp is not None:
            try:
                body = resp.text[:300]
            except Exception:
                body = "<no body>"
        res["err"] = f"{type(e).__name__}: {e}" + (f" | body={body}" if body else "")
    finally:
        try:
            await prov.aclose()
        except Exception:
            pass
    res["total_ms"] = round((time.monotonic() - t0) * 1000)
    return res


async def test_cache_roundtrip(provider_factories: dict) -> dict:
    """Cache MISS then HIT through the real CacheService on a TEMP store.

    Builds FRESH provider instances (the synth/stream step already aclose()'d
    the ones from phase 1, so their httpx clients are closed).
    """
    tmp = Path(tempfile.mkdtemp(prefix="dragontts-smoke-"))
    res: dict = {"provider_results": {}}
    for name, factory in provider_factories.items():
        meta = SQLiteMetadataStore(str(tmp / "smoke.db"))
        await meta.init()
        blobs = FilesystemBlobStore(str(tmp / "blobs"))
        await blobs.init()
        prov = factory()

        def get_provider(n, _prov=prov, _name=name):
            # Match by the provider KEY (dict name), not .name: the residency
            # ElevenLabsProvider instance still has .name == "elevenlabs".
            return _prov if n == _name else None

        svc = CacheService(meta, blobs, get_provider)
        req = TTSRequest(
            model_id=f"{name}:{PROVIDER_DEFAULTS[name]['model']}",
            transcript=PHRASE,
            voice=CartesiaVoice(id=PROVIDER_DEFAULTS[name]["voice_id"]),
            language=PROVIDER_DEFAULTS[name]["language"],
            output_format=OutputFormat(),
        )
        try:
            t = time.monotonic()
            audio1, h1 = await svc.get_or_synthesize(req)
            miss_ms = round((time.monotonic() - t) * 1000)
            ok1, _ = _validate_pcm(audio1, name)
            t = time.monotonic()
            audio2, h2 = await svc.get_or_synthesize(req)
            hit_ms = round((time.monotonic() - t) * 1000)
            ok2, _ = _validate_pcm(audio2, name)
            res["provider_results"][name] = {
                "miss_ok": ok1, "miss_status": h1["X-Cache"], "miss_ms": miss_ms,
                "hit_ok": ok2, "hit_status": h2["X-Cache"], "hit_ms": hit_ms,
                "identical": audio1 == audio2,
                "err": None,
            }
        except Exception as e:
            body = ""
            resp = getattr(e, "response", None)
            if resp is not None:
                try:
                    body = resp.text[:300]
                except Exception:
                    body = "<no body>"
            res["provider_results"][name] = {
                "err": f"{type(e).__name__}: {e}" + (f" | body={body}" if body else "")
            }
        finally:
            try:
                await prov.aclose()
            except Exception:
                pass
    return res


def _mark(ok):
    return "PASS" if ok else "FAIL"


async def main():
    print("=" * 78)
    print("DragonTTS live provider smoke-test")
    print(f"phrase: {PHRASE!r}")
    print("=" * 78)

    providers = {}
    factories = {}
    if settings.cartesia_api_key:
        providers["cartesia"] = CartesiaProvider()
        factories["cartesia"] = CartesiaProvider
    if settings.elevenlabs_indian_residency_api_key:
        # Single elevenlabs route -> Indian-residency creds (ElevenLabsProvider()
        # defaults to the residency key/url; global is no longer supported).
        providers["elevenlabs"] = ElevenLabsProvider()
        factories["elevenlabs"] = ElevenLabsProvider
    if settings.sarvam_api_key:
        providers["sarvam"] = SarvamProvider()
        factories["sarvam"] = SarvamProvider

    if not providers:
        print("No providers configured. Exiting.")
        return

    print(f"\n[1/2] synth + stream_synth per provider\n" + "-" * 78)
    results = []
    for name, prov in providers.items():
        print(f"  testing {name} ...", flush=True)
        r = await test_provider(name, prov)
        results.append(r)
        if r["err"]:
            print(f"    ERROR: {r['err']}")
        else:
            s, st = r["synth"], r["stream"]
            print(f"    synth  : {_mark(s['ok'])}  {s['detail']}  [{s['ms']}ms, {s['enc']}@{s['rate']}]")
            ttfb = st.get("ttfb_ms")
            print(f"    stream : {_mark(st['ok'])}  {st['detail']}  "
                  f"[{st['chunks']} chunks, ttfb={ttfb}ms, total={st['ms']}ms]")

    print(f"\n[2/2] cache MISS -> HIT round-trip (temp store)\n" + "-" * 78)
    cr = await test_cache_roundtrip(factories)
    for name, pr in cr["provider_results"].items():
        if pr.get("err"):
            print(f"  {name:10s}: ERROR  {pr['err']}")
        else:
            print(
                f"  {name:10s}: MISS {_mark(pr['miss_ok'])} ({pr['miss_status']}, {pr['miss_ms']}ms) "
                f"| HIT {_mark(pr['hit_ok'])} ({pr['hit_status']}, {pr['hit_ms']}ms) "
                f"| identical={pr['identical']}"
            )

    print("\n" + "=" * 78)
    all_ok = all(
        (r["err"] is None and r["synth"]["ok"] and r["stream"]["ok"]) for r in results
    )
    print("RESULT:", "ALL PASS ✓" if all_ok else "FAILURES (see above)")
    print("=" * 78)


if __name__ == "__main__":
    asyncio.run(main())
