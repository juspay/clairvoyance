"""Build the daily cache-economics Slack summary (overall + per provider).

Reuses the existing metrics rollups — no new storage:
- ``metrics_summary`` for the global requests/hits/words-served/synthesized,
- ``provider_metrics_summary`` for the per-provider breakdown (which now carries
  per-provider ``words_synthesized``, so per-provider words-from-cache % works),
- ``stats()`` for the cache snapshot (entries / total bytes),
- ``shutil.disk_usage`` for PVC utilization.

cost_saved(provider) = words_from_cache(provider) * settings.slack_cost_per_word[provider];
total cost saved = sum across providers (each provider has its own $/word).
"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.core.logging import logger


def _pct(num: int, den: int) -> str:
    if den <= 0:
        return "—"
    return f"{round(num * 100 / den)}%"


def _money(usd: float) -> str:
    # Display in INR, rounded to the nearest rupee (1 USD = slack_usd_to_inr).
    return f"₹{round(usd * settings.slack_usd_to_inr):,}"


def _human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def _today_window() -> tuple[str, str]:
    """(from, to) UTC dates covering roughly the day ending now: yesterday + today.
    At 11 PM IST (17:30 UTC) this captures the just-finished IST day well."""
    now = datetime.now(timezone.utc)
    return (now - timedelta(days=1)).date().isoformat(), now.date().isoformat()


async def build_summary(cache) -> dict:
    """Gather metrics and return ``{title, fields, sections, fallback_text}``
    shaped for :func:`app.alerts.slack.Alert.send`."""
    from_date, to_date = _today_window()
    meta = cache._metadata  # CacheService holds the metadata store
    m = await meta.metrics_summary(from_date=from_date, to_date=to_date)
    providers = await meta.provider_metrics_summary(from_date=from_date, to_date=to_date)
    snap = await meta.stats()

    requests = m.get("requests", 0)
    hits = m.get("hits", 0)
    words_served = m.get("words_served", 0)
    words_synthesized = m.get("words_synthesized", 0)
    words_from_cache = max(0, words_served - words_synthesized)

    rates = settings.slack_cost_per_word or {}
    total_cost = 0.0
    sections: list[dict] = []
    for prov, pm in sorted(providers.items()):
        p_req = pm.get("requests", 0)
        p_hits = pm.get("hits", 0)
        p_wserved = pm.get("words_served", 0)
        p_wsyn = pm.get("words_synthesized", 0)
        p_wfc = max(0, p_wserved - p_wsyn)
        cost = p_wfc * float(rates.get(prov, 0.0))
        total_cost += cost
        sections.append({
            "title": prov,
            "text": (
                f"• hit rate: *{_pct(p_hits, p_req)}* ({p_hits:,} / {p_req:,})\n"
                f"• words from cache: *{_pct(p_wfc, p_wserved)}* "
                f"({p_wfc:,} / {p_wserved:,})\n"
                f"• cost saved: *{_money(cost)}*"
            ),
        })

    fields = [
        {
            "name": "Cache hit rate",
            "value": f"{_pct(hits, requests)} ({hits:,} / {requests:,} requests)",
        },
        {
            "name": "Words from cache",
            "value": f"{_pct(words_from_cache, words_served)} "
                     f"({words_from_cache:,} / {words_served:,} words)",
        },
        {"name": "Est. cost saved", "value": f"{_money(total_cost)} (all providers)"},
        {"name": "Window", "value": f"{from_date} → {to_date} (UTC)"},
    ]

    # Cache snapshot + PVC usage (last section).
    entries = snap.get("entries", 0)
    total_bytes = snap.get("total_bytes", 0)
    try:
        du = shutil.disk_usage(settings.db_path)
        used_pct = round(du.used * 100 / du.total) if du.total else 0
        pvc = f"{_human_bytes(du.used)} / {_human_bytes(du.total)} ({used_pct}%)"
    except Exception:
        pvc = "—"
    sections.append({
        "title": "Cache",
        "text": (
            f"• entries: *{entries:,}*\n"
            f"• size: *{_human_bytes(total_bytes)}*\n"
            f"• PVC: *{pvc}*"
        ),
    })

    display_date = datetime.now(timezone.utc).strftime("%d-%m-%y")
    return {
        "title": f"📊 DragonTTS Daily Cache Summary - {display_date}",
        "fields": fields,
        "sections": sections,
        "fallback_text": (
            f"DragonTTS daily: hit {_pct(hits, requests)}, "
            f"words from cache {_pct(words_from_cache, words_served)}, "
            f"saved {_money(total_cost)}"
        ),
    }


def _past_target_time() -> bool:
    """True if the current UTC time is at/after settings.slack_summary_time_utc
    (HH:MM) today. Combined with the once-per-day claim this fires the summary
    exactly once, shortly after the target time. A malformed/out-of-range value
    falls back to 17:30 UTC (11 PM IST) rather than raising on every tick."""
    now = datetime.now(timezone.utc)
    try:
        parts = settings.slack_summary_time_utc.split(":")
        hh, mm = int(parts[0]), int(parts[1])
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    except Exception:
        target = now.replace(hour=17, minute=30, second=0, microsecond=0)
    return now >= target


async def send_daily_summary(cache, *, force: bool = False) -> bool:
    """Build + send the daily Slack summary.

    - ``force=True`` (the manual trigger endpoint): bypasses both the time gate
      and the once-per-day claim — posts immediately.
    - ``force=False`` (the background loop): only posts when past the target time
      AND this worker wins the once-per-day SQLite claim. On a send failure it
      releases the claim so a later tick retries.

    Returns True if a message was sent. A missing webhook URL => no-op (feature
    off). Never raises.
    """
    if not settings.slack_webhook_url:
        return False
    from app.alerts.slack import slack_alert  # lazy: avoid any import cycle

    today = datetime.now(timezone.utc).date().isoformat()
    if not force:
        if not _past_target_time():
            return False
        if not await cache._metadata.claim_slack_summary(today):
            return False  # another worker already posted today

    # Build + send inside one guard so ANY failure (a transient DB error in
    # build_summary, a send exception, or a shutdown CancelledError) releases the
    # once-per-day claim — otherwise a blip right after claiming would silently
    # suppress the whole day's summary. `finally` runs even on CancelledError,
    # releasing the claim so other workers can still post; the cancel then
    # propagates and ends the task on shutdown.
    ok = False
    try:
        payload = await build_summary(cache)
        ok = await slack_alert.send(**payload)
    except Exception as e:  # noqa: BLE001 — log; the loop also swallows
        logger.error(f"Slack summary build/send raised: {e}")
    finally:
        if not force and not ok:
            try:
                await cache._metadata.release_slack_summary()
            except Exception:
                pass
    return ok
