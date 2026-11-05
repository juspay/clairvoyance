"""Live ElevenLabs connection metrics (see app/providers/elevenlabs_live.py)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.core.config import settings
from app.providers.elevenlabs import ElevenLabsProvider, pool_max_size
from app.providers.elevenlabs_live import LIVE

router = APIRouter()


def _limits(provider: ElevenLabsProvider | None) -> dict:
    """The configured caps, next to the live numbers they bound."""
    budget = provider.account_budget if provider is not None else None
    ttd = settings.elevenlabs_dialogue_pool_size
    classic = settings.elevenlabs_stream_pool_size
    return {
        "workers": settings.elevenlabs_workers,
        "account_rotation": budget is not None,
        "ttd_refresh": settings.elevenlabs_ttd_ws_refresh,
        "ttd_refresh_interval_s": max(5.0, settings.elevenlabs_ttd_ws_refresh_interval),
        # Per worker, per pool (one pool per voice / model / language / rate;
        # under rotation, per model / language / rate on every voice).
        "ttd_max_sockets_per_pool_per_worker": (
            None if budget is not None else pool_max_size(ttd)
        ),
        "classic_warm_sockets_per_worker": classic,
        "classic_max_sockets_per_pool_per_worker": pool_max_size(classic),
        "contexts_per_socket": {"ttd": 4, "classic": 5},
    }


@router.get("/stats/elevenlabs/live")
async def elevenlabs_live(request: Request) -> dict:
    """ElevenLabs connections right now, summed over the pod's workers.

    ``sockets`` — connected (``open``) and by state: ``busy`` (speaking),
    ``idle``, ``connecting`` (first connect), ``reconnecting`` (dropped, in
    backoff), ``retiring`` (replaced by the refresh, finishing its sentences),
    ``suspect`` (silent after a send, skipped for 30 s). ``in_flight`` —
    incoming /tts/bytes + /tts/stream requests being served (``requests``),
    sentences at ElevenLabs over either path (``elevenlabs``), and of those on
    a socket (``websocket``) / one-shot HTTP (``http``). ``slots`` — sentence
    slots on serving sockets. ``peaks`` — maxima of every in-flight value over
    the last 5 / 15 / 60 minutes and since start (pod-wide sampled ~1/s;
    ``workers[].peaks`` are exact per worker). ``rates`` — per event kind
    (``requests`` incoming, ``cache_hits``, ``cache_misses``,
    ``elevenlabs_requests`` / ``_websocket`` / ``_http``): total, this / last
    minute, counts per window, ``peak_per_minute`` (exact over the last hour)
    and ``peak_per_second``. ``cache.hit_rate_pct`` — share served from the
    cache per window. ``by_account`` — under rotation, with each account's
    limits. ``counters`` — since the workers started.
    """
    if not settings.elevenlabs_live_metrics:
        raise HTTPException(status_code=404, detail="live metrics disabled")
    provider = request.app.state.registry.get("elevenlabs")
    if not isinstance(provider, ElevenLabsProvider):
        provider = None
    return {**LIVE.pod(), "limits": _limits(provider)}
