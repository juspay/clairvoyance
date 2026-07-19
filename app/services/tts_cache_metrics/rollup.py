"""Rollup: Redis TTS-cache day counters -> ``tts_cache_daily`` upserts.

Registered as the ``bb_ttscache_rollup`` background task. The scheduler's
distributed lock makes each tick single-pod. Every tick upserts today AND
yesterday (IST): yesterday's re-upsert finalizes the finished day right after
midnight and self-heals a missed tick — the upsert writes absolute day totals,
so re-running is idempotent.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from app.core.logger import logger
from app.database.accessor.breeze_buddy.tts_cache import upsert_tts_cache_daily
from app.services.tts_cache_metrics import IST, ist_date_str, read_day_counters


async def rollup_tts_cache_daily() -> None:
    """Upsert Redis day counters into ``tts_cache_daily`` for today + yesterday."""
    now_ist = datetime.now(IST)
    for date_str in (ist_date_str(now_ist), ist_date_str(now_ist - timedelta(days=1))):
        try:
            counters = await read_day_counters(date_str)
        except Exception as e:
            logger.error(f"ttscache rollup: reading {date_str} failed: {e}")
            continue
        if not counters:
            continue
        upserted = failed = 0
        for (provider, reseller_id, merchant_id, template_id), m in counters.items():
            try:
                await upsert_tts_cache_daily(
                    date_ist=date.fromisoformat(date_str),
                    provider=provider,
                    reseller_id=reseller_id,
                    merchant_id=merchant_id,
                    template_id=template_id,
                    hit_requests=m.get("hit_req", 0),
                    miss_requests=m.get("miss_req", 0),
                    hit_words=m.get("hit_words", 0),
                    miss_words=m.get("miss_words", 0),
                )
                upserted += 1
            except Exception:
                # Already logged (with context) by the accessor; keep going so
                # one bad row can't block the rest of the day's combos.
                failed += 1
        logger.info(
            f"ttscache rollup {date_str}: {upserted} rows upserted"
            + (f", {failed} failed" if failed else "")
        )
