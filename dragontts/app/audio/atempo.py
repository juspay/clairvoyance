"""ffmpeg atempo tempo stage — pitch-preserving speaking-rate control.

Applies to the ElevenLabs ``eleven_v3_conversational`` model ONLY, and only
when the effective tempo differs from 1.0: a tempo of exactly 1.0 (whether
absent, the default, or explicitly sent) is a pure bypass — ffmpeg is never
spawned, so ordinary traffic pays zero cost.

Two entry points:

- :func:`atempo_bytes` — one-shot stretch of a whole PCM clip (the ``_synth_v3``
  batch path). Stretched bytes are what the cache stores, so a hit serves the
  tempo-adjusted audio with no repeat cost.
- :func:`atempo_stream` — wraps an async PCM chunk iterator through a
  persistent ``ffmpeg`` process (stdin pipe in, stdout pipe out). Consumers
  see stretched chunks live, and whatever they accumulate (cache-during-stream)
  is the stretched audio — identical bytes to the batch path.

Failure policy: if ffmpeg cannot start (missing binary, cold PATH) the stream
degrades to passthrough — the call survives, just unstretched, with an error
log. If ffmpeg dies mid-stream (rare: OOM/kill), the already-yielded stretched
prefix stands and the remaining input is relayed unstretched (one speed
discontinuity beats silence). Upstream provider errors (e.g. pool
``SocketUnavailable``) propagate unchanged so stream_synth's one-shot fallback
keeps working.

Input is raw ``s16le`` mono PCM; output is the same format and rate — atempo
changes duration only, so the cache's requested-vs-native format matching is
unaffected.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

from app.core.logging import logger

#: ffmpeg atempo bounds (values outside this range 4xx in the filter).
ATEMPO_MIN = 0.5
ATEMPO_MAX = 2.0


def atempo_available() -> bool:
    """True when an ffmpeg binary is on PATH."""
    return shutil.which("ffmpeg") is not None


def _ffmpeg_args(sample_rate: int, tempo: float) -> list[str]:
    return [
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "s16le",
        "-ar",
        str(sample_rate),
        "-ac",
        "1",
        "-i",
        "pipe:0",
        "-af",
        f"atempo={tempo}",
        # Explicit output format flags: ffmpeg cannot infer them for pipe:1.
        "-f",
        "s16le",
        "-ar",
        str(sample_rate),
        "-ac",
        "1",
        "pipe:1",
    ]


async def _spawn(sample_rate: int, tempo: float) -> asyncio.subprocess.Process | None:
    try:
        return await asyncio.create_subprocess_exec(
            "ffmpeg",
            *_ffmpeg_args(sample_rate, tempo),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as exc:  # FileNotFoundError (no binary) or spawn failure
        logger.error(
            f"atempo: ffmpeg spawn failed ({exc}) — passing audio through unstretched"
        )
        return None


async def atempo_bytes(pcm: bytes, sample_rate: int, tempo: float) -> bytes:
    """One-shot stretch of a complete PCM clip.

    Raises:
        RuntimeError: ffmpeg failed or produced no output — the caller decides
            whether to serve the unstretched clip.
    """
    proc = await _spawn(sample_rate, tempo)
    if proc is None:
        raise RuntimeError("ffmpeg unavailable")
    stdout, stderr = await proc.communicate(pcm)
    if proc.returncode != 0 or not stdout:
        detail = stderr.decode(errors="replace")[:200] if stderr else ""
        raise RuntimeError(f"ffmpeg atempo failed rc={proc.returncode}: {detail}")
    return stdout


async def atempo_stream(
    chunks: AsyncIterator[bytes],
    sample_rate: int,
    tempo: float,
) -> AsyncGenerator[bytes, None]:
    """Pipe an async PCM chunk stream through a persistent ffmpeg atempo process.

    Passthrough semantics:
    - spawn failure → yield input chunks unchanged;
    - mid-stream ffmpeg death → stretched prefix stands, remaining input is
      relayed unstretched;
    - upstream errors from ``chunks`` propagate to the consumer unchanged.
    """
    proc = await _spawn(sample_rate, tempo)
    if proc is None:
        async for chunk in chunks:
            yield chunk
        return

    out_q: asyncio.Queue[Any] = asyncio.Queue()
    _SENTINEL = object()  # stdout closed — clean end
    ffmpeg_dead = False  # set when ffmpeg dies mid-stream; relay input raw

    async def pump_out() -> None:
        try:
            while True:
                data = await proc.stdout.read(16384)
                if not data:
                    break
                await out_q.put(data)
        except Exception as exc:  # noqa: BLE001 — reader must never kill the stream
            logger.error(f"atempo: stdout reader failed: {exc}")
        finally:
            await out_q.put(_SENTINEL)

    async def pump_in() -> None:
        nonlocal ffmpeg_dead
        try:
            async for chunk in chunks:
                if ffmpeg_dead:
                    await out_q.put(chunk)
                    continue
                proc.stdin.write(chunk)
                await proc.stdin.drain()
        except BrokenPipeError:
            ffmpeg_dead = True
            logger.error(
                "atempo: ffmpeg died mid-stream — continuing unstretched "
                "(the already-streamed prefix keeps its tempo)"
            )
            # Drain the rest of `chunks` straight to the consumer; a provider
            # error raised here still propagates via the queue below.
            try:
                async for chunk in chunks:
                    await out_q.put(chunk)
            except Exception as exc:  # noqa: BLE001 — forward upstream errors
                await out_q.put(exc)
        except Exception as exc:  # noqa: BLE001 — upstream (pool) errors propagate
            await out_q.put(exc)
        finally:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except Exception:  # noqa: BLE001 — best-effort close
                    pass

    reader = asyncio.create_task(pump_out())
    feeder = asyncio.create_task(pump_in())
    try:
        while True:
            item = await out_q.get()
            if item is _SENTINEL:
                break
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        for task in (reader, feeder):
            task.cancel()
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
