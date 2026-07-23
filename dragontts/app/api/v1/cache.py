"""Cache admin endpoints — list, fetch, and delete by raw key (for browsing)."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel

from app.alerts.summary import send_daily_summary
from app.audio.format import content_type_for
from app.schemas.cache import CacheEntryInfo, PaginatedCache

router = APIRouter()


def _to_info(r) -> CacheEntryInfo:
    return CacheEntryInfo(
        key=r.key,
        text=r.text,
        provider=r.provider,
        voice_id=r.voice_id,
        model=r.model,
        language=r.language,
        container=r.container,
        encoding=r.encoding,
        sample_rate=r.sample_rate,
        size_bytes=r.size_bytes,
        hit_count=r.hit_count,
        created_at=r.created_at,
    )


MAX_CACHE_LIST_LIMIT = 1000  # hard cap so a list call can never stream the whole DB


@router.get("/cache", response_model=PaginatedCache)
async def list_cache(
    request: Request,
    provider: str | None = None,
    voice_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
    q: str | None = Query(default=None, description="filter by text (see match)"),
    match: str = Query(default="exact", description="'exact' (default) or 'substring'"),
    created_after: str | None = Query(
        default=None, description="YYYY-MM-DD, inclusive"
    ),
    created_before: str | None = Query(
        default=None, description="YYYY-MM-DD, exclusive"
    ),
):
    """Paginated entry listing with optional text/date filters. limit is clamped
    to [1, MAX_CACHE_LIST_LIMIT]. q+match filters text; created_after is
    inclusive, created_before is exclusive (both YYYY-MM-DD)."""
    metadata = request.app.state.metadata
    limit = max(1, min(limit, MAX_CACHE_LIST_LIMIT))
    offset = max(0, offset)
    # Fetch one extra row to detect a next page without a COUNT(*) scan.
    records = await metadata.list(
        provider=provider,
        voice_id=voice_id,
        limit=limit + 1,
        offset=offset,
        q=q,
        match=match,
        created_before=created_before,
        created_after=created_after,
    )
    has_next = len(records) > limit
    entries = [_to_info(r) for r in records[:limit]]
    return PaginatedCache(
        entries=entries, offset=offset, limit=limit, has_next=has_next
    )


@router.get("/stats")
async def stats(
    request: Request,
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
):
    """Cache metrics. Request metrics are date-filterable (?from=&to= YYYY-MM-DD);
    the cache snapshot is the current point-in-time state. All from stored
    rollups/totals — no full-table scan per call."""
    for label, value in (("from", from_date), ("to", to_date)):
        if value is not None:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(
                    status_code=400, detail=f"invalid {label}; use YYYY-MM-DD"
                )
    metadata = request.app.state.metadata
    registry = request.app.state.registry
    metrics = await metadata.metrics_summary(from_date=from_date, to_date=to_date)
    snapshot = await metadata.stats()
    providers = await metadata.provider_metrics_summary(
        from_date=from_date, to_date=to_date
    )
    latency = await metadata.latency_summary(from_date=from_date, to_date=to_date)
    return {
        "range": {"from": from_date, "to": to_date},
        **metrics,
        "providers": providers,
        "latency": latency,
        "entries": snapshot["entries"],
        "total_bytes": snapshot["total_bytes"],
        "total_words": snapshot["total_words"],
        "by_provider": snapshot["by_provider"],
        "providers_configured": registry.configured(),
        "session": request.app.state.cache.session_stats,
    }


@router.get("/stats/daily")
async def stats_daily(
    request: Request,
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
    provider: str | None = None,
):
    """Extensive day-wise analytics: per date, the full metric totals (hit_rate,
    words_from_cache_pct derived) plus a per-provider breakdown. ``provider``
    narrows the view to one provider (its totals then reflect that provider
    only). ?from=&to= YYYY-MM-DD (UTC dates)."""
    for label, value in (("from", from_date), ("to", to_date)):
        if value is not None:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(
                    status_code=400, detail=f"invalid {label}; use YYYY-MM-DD"
                )
    cache = request.app.state.cache
    return await cache.daily_stats(
        from_date=from_date, to_date=to_date, provider=provider
    )


@router.get("/stats/latency")
async def stats_latency(
    request: Request,
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
    provider: str | None = None,
):
    """Per-provider x per-day latency (avg/p95/count, microseconds):
    ``synth`` (provider synth time, MISS-only), ``total`` (end-to-end),
    ``cache_serve`` (HIT serve), ``ttfb`` (stream first byte), plus a derived
    ``miss_overhead_us`` = total.avg - synth.avg (DragonTTS work beyond the
    provider call). ?from=&to= YYYY-MM-DD (UTC); ?provider= narrows to one."""
    for label, value in (("from", from_date), ("to", to_date)):
        if value is not None:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(
                    status_code=400, detail=f"invalid {label}; use YYYY-MM-DD"
                )
    metadata = request.app.state.metadata
    providers = await metadata.latency_summary_by_provider(
        from_date=from_date, to_date=to_date, provider=provider
    )
    # Derive the MISS overhead per day: total.avg - synth.avg. Population estimate
    # only -- synth and total are sampled independently per kind, so the same
    # request isn't guaranteed to appear in both populations.
    for _prov, days in providers.items():
        for _date, kinds in days.items():
            total_avg = kinds.get("total", {}).get("avg_us")
            synth_avg = kinds.get("synth", {}).get("avg_us")
            kinds["miss_overhead_us"] = (
                round(total_avg - synth_avg, 1)
                if total_avg is not None and synth_avg is not None
                else None
            )
    return {
        "range": {"from": from_date, "to": to_date, "provider": provider},
        "providers": providers,
        "note": (
            "miss_overhead_us = total.avg - synth.avg (population estimate; synth "
            "and total are independently sampled). synth = provider synth time "
            "(MISS-only); total = end-to-end; cache_serve = HIT serve; "
            "ttfb = stream time-to-first-byte."
        ),
    }


@router.post("/cache/clear")
async def clear_cache(
    request: Request,
    provider: str | None = None,
    voice_id: str | None = None,
    dry_run: bool = False,
):
    """Delete all entries (optionally filtered by provider/voice_id). Pass
    ``dry_run=true`` to preview the count/bytes/providers without deleting."""
    cache = request.app.state.cache
    if dry_run:
        preview = await cache.clear_preview(provider=provider, voice_id=voice_id)
        return {"status": "preview", **preview}
    count = await cache.clear(provider=provider, voice_id=voice_id)
    return {"status": "cleared", "deleted": count}


class DeleteByTextBody(BaseModel):
    """Body for POST /cache/delete-by-text."""

    text: str | None = None
    provider: str | None = None
    voice_id: str | None = None
    match: str = "exact"  # 'exact' (default) or 'substring'
    dry_run: bool = True  # safe by default — set False to actually delete


@router.post("/cache/delete-by-text")
async def delete_by_text(body: DeleteByTextBody, request: Request):
    """Delete cache entries by text (exact or substring), optionally narrowed by
    provider/voice_id. ``dry_run`` defaults True — previews matched entries +
    keys without deleting; set ``dry_run: false`` to delete. Requires at least
    one filter (text/provider/voice_id) — use /cache/clear to wipe everything."""
    cache = request.app.state.cache
    try:
        return await cache.delete_by_text(
            text=body.text,
            provider=body.provider,
            voice_id=body.voice_id,
            match=body.match,
            dry_run=body.dry_run,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/cache/delete-by-age")
async def delete_by_age(
    request: Request,
    older_than_days: int = Query(..., description="evict entries older than N days"),
    dry_run: bool = Query(True, description="preview unless set to false"),
):
    """Delete entries older than ``older_than_days`` (by created_at). ``dry_run``
    defaults True — preview matched entries without deleting."""
    cache = request.app.state.cache
    return await cache.delete_by_age(older_than_days=older_than_days, dry_run=dry_run)


@router.post("/cache/backfill-ttl")
async def backfill_ttl(request: Request):
    """One-shot: assign a random 48–72h expiry to pre-existing entries that have
    no TTL (created before length-scaled TTL, e.g. under ttl_seconds=0) so they
    age out instead of living forever. Idempotent — only touches NULL-TTL rows,
    so it's safe to call repeatedly. Run once after deploying this feature."""
    cache = request.app.state.cache
    count = await cache.backfill_missing_ttl()
    return {"status": "backfilled", "updated": count}


@router.post("/slack-summary")
async def slack_summary(request: Request):
    """Force-send the daily Slack cache summary now — bypasses the time gate and
    the once-per-day claim. Use to test the message or recover a missed day. A
    missing webhook URL is a no-op (returns ``sent=false``)."""
    cache = request.app.state.cache
    sent = await send_daily_summary(cache, force=True)
    return {"sent": sent}


@router.get("/cache/{key}")
async def get_cache(key: str, request: Request):
    metadata = request.app.state.metadata
    blobs = request.app.state.blobs
    record = await metadata.get(key)
    if not record:
        raise HTTPException(status_code=404, detail="cache key not found")
    audio = await blobs.get(record.storage_path)
    return Response(
        content=audio,
        media_type=content_type_for(record.encoding),
        headers={"X-Provider": record.provider, "X-Voice-Id": record.voice_id},
    )


@router.delete("/cache/{key}")
async def delete_cache(key: str, request: Request):
    metadata = request.app.state.metadata
    blobs = request.app.state.blobs
    record = await metadata.get(key)
    if not record:
        raise HTTPException(status_code=404, detail="cache key not found")
    await metadata.delete(key)
    await blobs.delete(record.storage_path)
    return {"status": "deleted", "key": key}
