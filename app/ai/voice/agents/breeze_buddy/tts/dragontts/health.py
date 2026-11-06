"""DragonTTS health flag — the underlying state machine.

Owns the ``dragontts:health`` Redis flag and its primitives: a one-shot probe
(``check_dragontts_health``), the call-time gate (``is_dragontts_healthy``),
and the internal flag writer (``_set_health``). It is imported by the call-time
TTS path (``tts/__init__``), so it MUST NOT import the ``dispatch`` package —
that would form a cycle (``dispatch`` -> ``worker`` -> ``managers.utils`` ->
``tts/__init__``). The operator-facing kill/restore actions live in
``kill_switch.py``; the scheduler task that drives the probe + pages on failure
lives in ``monitor.py`` (imported only by ``app.main``).

On a failed probe, ``check_dragontts_health`` marks DragonTTS unhealthy by
setting ``dragontts:health = "0"`` — **one-way**: the gate only ever marks
down (1->0), never up. At call time, templates with
``tts_configuration.enable_tts_caching=true`` route through DragonTTS unless
the flag is ``"0"``, in which case they use their upstream provider directly.

Flag values: ``"0"`` = unhealthy (bypass caching); absent or ``"1"`` = healthy
(use DragonTTS). A missing key defaults to healthy so a fresh deploy caches
immediately, before the first probe.
"""

from __future__ import annotations

import httpx

from app.core.config.dynamic import DRAGONTTS_HEALTH_TIMEOUT_S, DRAGONTTS_URL
from app.core.logger import logger
from app.services.redis import get_redis_service

__all__ = [
    "check_dragontts_health",
    "is_dragontts_healthy",
]

# "0" = unhealthy (bypass DragonTTS for enable_tts_caching templates);
# absent / "1" = healthy (route through DragonTTS).
_HEALTH_KEY = "dragontts:health"


async def check_dragontts_health() -> bool:
    """Probe DragonTTS ``/health``. On failure, mark it unhealthy (one-way
    1->0). On success, do nothing — recovery is manual via the admin endpoint.

    Returns the probe result. A 2xx response is healthy; anything else
    (non-2xx, timeout, connection error) is unhealthy.
    """
    health_url = f"{await DRAGONTTS_URL()}/health"
    timeout = await DRAGONTTS_HEALTH_TIMEOUT_S()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(health_url)
            healthy = response.is_success
            if not healthy:
                logger.warning(
                    f"DragonTTS health probe returned HTTP {response.status_code}"
                )
    except Exception as e:  # noqa: BLE001 — any probe failure = unhealthy
        logger.warning(f"DragonTTS health probe failed: {e}")
        healthy = False

    if not healthy:
        # One-way: only mark down. A subsequent admin restore flips it back up.
        await _set_health(False)
    return healthy


async def _set_health(healthy: bool) -> None:
    try:
        redis = await get_redis_service()
        await redis.set(_HEALTH_KEY, "1" if healthy else "0")
    except Exception as e:  # noqa: BLE001 — flag write is best-effort
        logger.warning(f"Failed to set DragonTTS health flag: {e}")


async def is_dragontts_healthy() -> bool:
    """``True`` unless the flag is explicitly ``"0"`` (absent/``"1"`` => healthy).

    A Redis read error returns ``True`` (keep caching) — consistent with the
    "missing = healthy" default; the only bypass signal is an explicit ``"0"``.
    """
    try:
        redis = await get_redis_service()
        return await redis.get(_HEALTH_KEY) != "0"
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Failed to read DragonTTS health flag: {e}")
        return True
