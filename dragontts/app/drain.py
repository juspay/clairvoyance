"""Graceful-drain coordinator: tell clairvoyance to bypass us, then drain.

k8s termination sequence we participate in:

1. preStop hook -> ``GET /drain`` (this runs ONCE, served by one worker). In
   strict order we: (a) enable the kill switch — mark draining (readiness 503)
   and POST clairvoyance's admin "kill_switch" so its ``enable_tts_caching``
   templates bypass dragontts and use their upstream provider — THEN (b) send
   the Slack alert. No new TTS traffic reaches us.
2. SIGTERM -> uvicorn stops accepting, lifespan shutdown runs in EVERY worker.
   Each worker waits for its own in-flight requests (the gauge below) to finish
   up to ``settings.graceful_drain_max_seconds``, then the existing cleanup
   (metrics flush, checkpoint, pool close) proceeds.
3. Restore is MANUAL: an operator POSTs ``action=restore`` to clairvoyance's
   admin endpoint. dragontts does NOT auto-restore on startup, so caching stays
   bypassed (upstream TTS) until someone explicitly re-enables it.

The drain flag + Slack + clairvoyance call are once-only by design, so it's fine
that ``/drain`` is handled by a single worker. The in-flight gauge is per
process (per worker), which is exactly the granularity each worker drains on
SIGTERM — see app/main.py.
"""

from __future__ import annotations

import asyncio
import os

import httpx

from app.core.config import settings
from app.core.logging import logger

# --- drain flag (process-local) ---------------------------------------------
# Read by GET /ready (503 while draining) so k8s pulls the pod out of the
# Service endpoints. Best-effort across the 4 workers (only the one that served
# /drain sets it); k8s removes a terminating pod from endpoints regardless, so
# this is belt-and-suspenders, not the primary new-traffic gate.
_is_draining: bool = False

# --- in-flight gauge (process-local) ----------------------------------------
# Inc'd by the HTTP middleware around every request, dec'd on completion. The
# SIGTERM shutdown path waits for this to reach 0 (capped) so live /tts/stream
# responses finish before pools/db close. Single event loop => plain int is safe
# (inc/dec happen between awaits, atomically at the Python level).
_inflight: int = 0


def is_draining() -> bool:
    return _is_draining


def mark_draining() -> None:
    global _is_draining
    _is_draining = True


def incr_inflight() -> None:
    global _inflight
    _inflight += 1


def decr_inflight() -> None:
    global _inflight
    if _inflight > 0:
        _inflight -= 1


def inflight() -> int:
    return _inflight


async def notify_clairvoyance() -> bool:
    """POST clairvoyance's dragontts manage endpoint to ENABLE the kill switch.

    One-way: dragontts only ever engages the kill switch (bypass). Restore is a
    manual operator action on clairvoyance's admin endpoint — never done here.
    Best-effort: never raises. Returns True on a 2xx, False otherwise. A missing
    ``clairvoyance_url``/token is a clean no-op (feature degrades off), so an
    unconfigured or dev box never depends on clairvoyance being reachable.
    """
    if not settings.clairvoyance_url or not settings.clairvoyance_jwt_token:
        logger.info("clairvoyance kill-switch skipped (CLAIRVOYANCE_URL/JWT not set)")
        return False
    url = settings.clairvoyance_url.rstrip("/") + settings.clairvoyance_manage_path
    headers = {"Authorization": f"Bearer {settings.clairvoyance_jwt_token}"}
    try:
        async with httpx.AsyncClient(
            timeout=settings.clairvoyance_kill_switch_timeout
        ) as client:
            resp = await client.post(
                url, json={"action": "kill_switch"}, headers=headers
            )
        ok = resp.is_success
        (logger.info if ok else logger.warning)(
            f"clairvoyance kill-switch -> HTTP {resp.status_code} "
            f"({'ok' if ok else resp.text[:160]})"
        )
        return ok
    except Exception as e:  # noqa: BLE001 — best-effort; monitor is the backstop
        logger.warning(f"clairvoyance kill-switch failed: {e}")
        return False


async def send_kill_switch_slack(*, clairvoyance_ok: bool, inflight_n: int) -> None:
    """Fire the 🛑 kill-switch-activated Slack alert (best-effort, never raises)."""
    try:
        from app.alerts.slack import slack_alert  # lazy: avoid any import cycle

        pod = os.environ.get("HOSTNAME", "unknown")
        await slack_alert.send(
            title="🛑 DragonTTS kill switch activated before pod shutdown",
            fields=[
                {
                    "name": "Trigger",
                    "value": (
                        "Activated by DragonTTS itself on its preStop /drain hook "
                        "— the pod is shutting down next"
                    ),
                },
                {
                    "name": "Clairvoyance bypass",
                    "value": (
                        "sent ✓"
                        if clairvoyance_ok
                        else "failed/skipped (monitor is backstop)"
                    ),
                },
                {"name": "In-flight at trigger", "value": f"{inflight_n} request(s)"},
                {"name": "Pod", "value": pod},
            ],
            fallback_text=(
                f"DragonTTS kill switch activated by DragonTTS before pod "
                f"shutdown on {pod} "
                f"(clairvoyance bypass={'sent' if clairvoyance_ok else 'failed'}, "
                f"{inflight_n} in-flight)"
            ),
        )
    except Exception as e:  # noqa: BLE001 — never block drain on an alert
        logger.warning(f"kill-switch Slack alert failed: {e}")


async def engage_drain() -> bool:
    """Run the /drain sequence, strictly ordered:

    1. enable the kill switch — mark draining (readiness 503) + tell clairvoyance
       to bypass us so no new TTS calls arrive,
    2. send the Slack alert,
    3. (SIGTERM, in lifespan shutdown) drain in-flight requests,
    4. shutdown (pool/db cleanup).

    The alert is AWAITED (bounded) so it's guaranteed sent before draining
    begins, but a slow/down webhook can't stall preStop past the grace budget.
    """
    if not settings.enable_graceful_drain:
        logger.info("/drain skipped (ENABLE_GRACEFUL_DRAIN=false)")
        return False
    if _is_draining:
        logger.info("/drain already engaged — no-op")
        return False
    # 1) Kill switch FIRST.
    mark_draining()
    clairvoyance_ok = await notify_clairvoyance()
    # 2) Alert (after the kill switch is enabled).
    try:
        await asyncio.wait_for(
            send_kill_switch_slack(
                clairvoyance_ok=clairvoyance_ok, inflight_n=inflight()
            ),
            timeout=2.0,
        )
    except asyncio.TimeoutError:
        logger.warning("kill-switch Slack alert timed out — proceeding with drain")
    return clairvoyance_ok


async def wait_for_inflight_drain() -> None:
    """On SIGTERM: wait for in-flight requests to finish, capped at the budget."""
    if not settings.enable_graceful_drain:
        return
    budget = settings.graceful_drain_max_seconds
    # Poll at a fine cadence so short requests finish quickly and we don't hold
    # the full budget when there's nothing left to drain.
    deadline = asyncio.get_running_loop().time() + budget
    while _inflight > 0 and asyncio.get_running_loop().time() < deadline:
        logger.info(f"shutdown drain: waiting on {_inflight} in-flight request(s)")
        await asyncio.sleep(0.25)
    if _inflight > 0:
        logger.warning(
            f"shutdown drain: {_inflight} request(s) still in-flight after "
            f"{budget}s budget — proceeding with cleanup"
        )
    else:
        logger.info("shutdown drain: all in-flight requests complete")
