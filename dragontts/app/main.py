"""DragonTTS FastAPI application.

Lifespan wires up the metadata store (SQLite), blob store (filesystem), and the
provider registry (built from configured API keys), then hands a CacheService
to the routers via ``app.state``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import ctypes
import gc
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1 import cache as cache_api, health, tts
from app.cache.service import CacheService
from app.core.config import settings
from app.core.logging import logger
from app.providers.registry import ProviderRegistry
from app.storage.filesystem import FilesystemBlobStore
from app.storage.sqlite import SQLiteMetadataStore


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Route uvicorn's own loggers (access/error) through the loguru sink so GCP
    # labels their severity too — they set propagate=False, so the root
    # InterceptHandler alone misses them; override their handlers at startup.
    import logging as _logging

    from app.core.logging import InterceptHandler

    for _name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        _lg = _logging.getLogger(_name)
        _lg.handlers = [InterceptHandler()]
        _lg.propagate = False

    # Sized executor for asyncio.to_thread (blocking sqlite/file I/O) so many
    # concurrent requests don't queue on the tiny default pool.
    loop = asyncio.get_running_loop()
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=settings.thread_pool_workers
    )
    loop.set_default_executor(executor)

    metadata = SQLiteMetadataStore(settings.db_path)
    await metadata.init()

    blobs = FilesystemBlobStore(settings.blob_dir)
    await blobs.init()

    registry = ProviderRegistry()
    registry.build()
    await registry.warm()  # pre-warm the Cartesia streaming socket pool

    app.state.metadata = metadata
    app.state.blobs = blobs
    app.state.registry = registry
    cache = CacheService(metadata, blobs, registry.get)
    await cache.start()  # write-behind metrics flusher
    app.state.cache = cache

    # Reap blob files orphaned by a prior crash mid-delete/clear (idempotent).
    try:
        await cache.reconcile_blobs()
    except Exception as e:
        logger.warning(f"blob reconcile failed: {e}")

    # Periodic WAL checkpoint so the -wal file stays bounded on the PVC while
    # worker connections are held open (passive auto-checkpoint won't shrink it).
    async def _checkpoint_loop():
        while True:
            await asyncio.sleep(300)
            try:
                await metadata.checkpoint()
                await metadata.prune_latency(settings.metrics_latency_retention_days)
            except Exception as e:
                logger.debug(f"periodic maintenance failed: {e}")

    checkpoint_task = asyncio.create_task(_checkpoint_loop())

    # Periodic TTL purge: delete expired entries (rows + blobs). Cadence is
    # coarse (default 20 min) since TTLs are hour-to-day scale. Backfill of
    # pre-existing NULL-TTL entries is NOT automatic — trigger it once via
    # POST /cache/backfill-ttl after deploying this feature.
    async def _ttl_purge_loop():
        while True:
            await asyncio.sleep(settings.ttl_purge_interval_seconds)
            try:
                await cache.purge_expired()
            except Exception as e:
                logger.debug(f"TTL purge failed: {e}")

    ttl_purge_task = asyncio.create_task(_ttl_purge_loop())

    # Once-daily Slack cache-economics summary (overall + per-provider hit rate,
    # words-from-cache %, est. cost saved) at slack_summary_time_utc. Webhook
    # absent => feature off (send_daily_summary is a no-op). Never fatal.
    async def _slack_summary_loop():
        while True:
            await asyncio.sleep(settings.slack_summary_tick_seconds)
            try:
                from app.alerts.summary import send_daily_summary

                await send_daily_summary(cache)
            except Exception as e:
                logger.debug(f"Slack summary tick failed: {e}")

    slack_summary_task = asyncio.create_task(_slack_summary_loop())

    # Periodic glibc malloc_trim: return freed heap (the large short-lived audio
    # + numpy resample buffers) to the OS so RSS doesn't plateau high. A no-op
    # when nothing is trimmable; skipped silently on non-glibc (musl). No effect
    # on audio quality or concurrency — it only releases already-freed heap and
    # touches no synth/resample/worker/pool path. Runs once per worker process.
    async def _malloc_trim_loop():
        if not settings.malloc_trim_enabled:
            return
        try:
            libc = ctypes.CDLL("libc.so.6")
            trim = getattr(libc, "malloc_trim", None)
        except OSError:
            logger.warning(
                "malloc_trim: libc.so.6 not found — trim disabled (non-glibc?)"
            )
            return
        if trim is None:
            return
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        while True:
            try:
                await asyncio.sleep(settings.malloc_trim_interval_seconds)
                gc.collect()  # drop unreachable Python objects pinning C buffers
                trim(0)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"malloc_trim failed: {e}")

    malloc_trim_task = asyncio.create_task(_malloc_trim_loop())

    logger.info(f"DragonTTS ready — providers: {registry.configured()}")
    yield
    checkpoint_task.cancel()
    ttl_purge_task.cancel()
    slack_summary_task.cancel()
    malloc_trim_task.cancel()
    try:
        await checkpoint_task
    except (asyncio.CancelledError, Exception):
        pass
    try:
        await ttl_purge_task
    except (asyncio.CancelledError, Exception):
        pass
    try:
        await slack_summary_task
    except (asyncio.CancelledError, Exception):
        pass
    try:
        await malloc_trim_task
    except (asyncio.CancelledError, Exception):
        pass
    await cache.stop()  # flush write-behind metrics (graceful shutdown loses none)
    try:
        await metadata.checkpoint()  # compact the WAL before worker conns close
    except Exception:
        pass
    await registry.aclose_all()
    executor.shutdown(wait=False, cancel_futures=True)  # releases worker sqlite conns


app = FastAPI(title="DragonTTS", version="0.1.0", lifespan=lifespan)
app.include_router(tts.router)
app.include_router(cache_api.router)
app.include_router(health.router)
