"""Daily auto-restore of clairvoyance's DragonTTS health flag.

The flag is ONE-WAY by design. Clairvoyance's ~60s monitor only ever marks
DragonTTS *unhealthy* — its ``enable_tts_caching`` templates then bypass us and
speak through their upstream TTS — and dragontts itself only ever engages the
kill switch, on drain (:mod:`app.drain`). Nothing ever turned it back on, so a
transient blip (a rolling restart, a brief partition, an overnight deploy) left
the whole cache unused until a human noticed and POSTed ``action=restore`` by
hand. Between the blip and that human, every call paid full synth cost and
latency for greetings that were already cached.

This closes the loop on a schedule: once a day at ``health_restore_time_utc``
(default 00:30 UTC = 06:00 IST, before the calling window opens) read the flag
and, ONLY if it reads "unhealthy", restore it.

FAILS CLOSED. A non-200 status, or a body whose ``health`` field we cannot
parse, means the real state is UNKNOWN — we do NOT restore. Re-enabling caching
against a DragonTTS we cannot read is worse than leaving it bypassed. When we
DO restore, clairvoyance's own ~60s monitor is the backstop: if the service is
genuinely broken it re-kills the flag within a minute, so the worst case is one
minute of degraded routing, not a silent outage.

Credentials are the SAME pair the drain kill-switch uses — ``CLAIRVOYANCE_URL``
and ``CLAIRVOYANCE_JWT_TOKEN`` — so there is one token and one rotation point.
Either unset => clean no-op, exactly like the kill switch, so a dev box never
depends on clairvoyance being reachable.

Coordination across the N uvicorn workers is the same once-per-day SQLite claim
the Slack summary uses (DragonTTS has no Redis), so exactly one worker acts.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from app.core.config import settings
from app.core.logging import logger
from app.storage.sqlite import HEALTH_RESTORE_CLAIM

#: Fallback when ``health_restore_time_utc`` is malformed — 06:00 IST.
_DEFAULT_HH, _DEFAULT_MM = 0, 30


def _past_target_time() -> bool:
    """True once the current UTC time is at/after ``health_restore_time_utc``
    (HH:MM) today. Combined with the once-per-day claim this fires the job
    exactly once, shortly after the target time. A malformed/out-of-range value
    falls back to 00:30 UTC rather than raising on every tick."""
    now = datetime.now(timezone.utc)
    try:
        parts = settings.health_restore_time_utc.split(":")
        hh, mm = int(parts[0]), int(parts[1])
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
    except Exception:
        hh, mm = _DEFAULT_HH, _DEFAULT_MM
    return now >= now.replace(hour=hh, minute=mm, second=0, microsecond=0)


def _configured() -> bool:
    return bool(settings.clairvoyance_url and settings.clairvoyance_jwt_token)


def _url(path: str) -> str:
    return settings.clairvoyance_url.rstrip("/") + path


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.clairvoyance_jwt_token}"}


async def fetch_health(client: httpx.AsyncClient) -> str | None:
    """Current flag: "healthy", "unhealthy", or None when UNKNOWN.

    None covers every case where we must not act — transport failure, non-200,
    unparseable body, or an unrecognized value. The caller treats None as
    "leave it alone".
    """
    try:
        resp = await client.get(
            _url(settings.clairvoyance_status_path), headers=_headers()
        )
    except Exception as e:  # noqa: BLE001 — unknown state, never raise on a tick
        logger.warning(f"dragontts health-restore: status request failed: {e}")
        return None
    if not resp.is_success:
        logger.warning(
            f"dragontts health-restore: status HTTP {resp.status_code} "
            f"({resp.text[:160]}) — state UNKNOWN, not restoring"
        )
        return None
    try:
        health = resp.json().get("health")
    except Exception:  # noqa: BLE001 — non-JSON body
        health = None
    if health not in ("healthy", "unhealthy"):
        logger.warning(
            f"dragontts health-restore: unparseable health={health!r} in "
            f"{resp.text[:160]} — state UNKNOWN, not restoring"
        )
        return None
    return health


async def restore_health(client: httpx.AsyncClient) -> bool:
    """POST ``action=restore``. Returns True on a 2xx. Never raises."""
    try:
        resp = await client.post(
            _url(settings.clairvoyance_manage_path),
            json={"action": "restore"},
            headers=_headers(),
        )
    except Exception as e:  # noqa: BLE001 — best-effort; a later day retries
        logger.error(f"dragontts health-restore: restore request failed: {e}")
        return False
    ok = resp.is_success
    (logger.info if ok else logger.error)(
        f"dragontts health-restore: restore -> HTTP {resp.status_code} "
        f"({'ok' if ok else resp.text[:160]})"
    )
    return ok


async def run_daily_restore(cache, *, force: bool = False) -> bool:
    """Read the flag and restore it if — and only if — it reads "unhealthy".

    - ``force=True``: bypasses both the time gate and the once-per-day claim
      (the manual trigger path).
    - ``force=False`` (the background loop): acts only when past the target
      time AND this worker wins the once-per-day claim. If the run does not
      conclude in a known-good state the claim is released so a later tick
      retries the same day.

    Returns True only when a restore was issued AND accepted. "Already healthy"
    returns False — nothing was done, which is the desired outcome, so callers
    should read the logs rather than the boolean for success. Never raises.
    """
    if not settings.health_restore_enabled or not _configured():
        return False
    if not force:
        if not _past_target_time():
            return False
        today = datetime.now(timezone.utc).date().isoformat()
        if not await cache._metadata.claim_daily(HEALTH_RESTORE_CLAIM, today):
            return False  # another worker already ran it today

    settled = False  # a known state was reached; no retry needed today
    try:
        async with httpx.AsyncClient(
            timeout=settings.clairvoyance_kill_switch_timeout
        ) as client:
            health = await fetch_health(client)
            if health is None:
                return False  # UNKNOWN — fail closed, retry on a later tick
            if health == "healthy":
                logger.info("dragontts health-restore: already healthy — no action")
                settled = True
                return False
            logger.warning(
                "dragontts health-restore: flag is UNHEALTHY — restoring "
                "(clairvoyance's ~60s monitor re-kills it if we are still broken)"
            )
            if not await restore_health(client):
                return False  # retry on a later tick
            after = await fetch_health(client)
            if after != "healthy":
                logger.error(
                    f"dragontts health-restore: restore accepted but flag reads "
                    f"{after!r} — will retry"
                )
                return False
            logger.info("dragontts health-restore: unhealthy -> healthy")
            settled = True
            return True
    finally:
        # Release the claim unless we reached a known-good state, so a blip
        # cannot silently burn the whole day's only attempt. `finally` runs on
        # CancelledError too (shutdown), which then propagates normally.
        if not force and not settled:
            try:
                await cache._metadata.release_daily(HEALTH_RESTORE_CLAIM)
            except Exception:  # noqa: BLE001 — best-effort
                pass
