"""TTS endpoints — Cartesia-mimic read + check/create/delete admin ops.

All take the same Cartesia-shaped body and operate by the derived cache key:
- POST /tts/bytes   read (HIT returns cached, MISS synth+store+return) — drop-in
- POST /tts/stream  read as a chunked stream (MISS streams from the provider as
                     it synthesizes, HIT streams the cached blob) — low TTFB
- POST /tts/check   existence check, no synthesis
- POST /tts/create  force synth-or-base64 store (overrides)
- POST /tts/create/bulk  batch warm a phrase library
- POST /tts/delete  remove by derived key

Upstream provider HTTP/network failures map to 502/503, never 500.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncGenerator

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from app.audio.format import content_type_for
from app.cache.key import parse_model_id
from app.cache.resilience import ProviderBusy
from app.core.config import settings
from app.core.logging import logger
from app.providers.base import ProviderError
from app.providers.registry import ProviderNotConfigured
from app.schemas.cache import CheckResponse, CreateResponse, DeleteResponse
from app.schemas.tts import TTSRequest

router = APIRouter()


def _map_upstream_error(provider: str, exc: Exception) -> HTTPException:
    if isinstance(exc, (httpx.HTTPStatusError, ProviderError)):
        return HTTPException(
            status_code=502,
            detail=f"upstream {provider} returned an error: {exc}",
        )
    return HTTPException(status_code=503, detail=f"upstream {provider} unreachable")


@router.post("/tts/bytes")
async def tts_bytes(req: TTSRequest, request: Request):
    cache = request.app.state.cache
    try:
        provider, _ = parse_model_id(req.model_id)
        audio, headers = await cache.get_or_synthesize(req)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ProviderNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ProviderBusy as e:
        raise HTTPException(status_code=503, detail=str(e), headers={"Retry-After": "1"})
    except ProviderError as e:
        raise HTTPException(
            status_code=502, detail=f"upstream {provider} returned an error: {e}"
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        raise _map_upstream_error(provider, e)

    return Response(
        content=audio,
        media_type=content_type_for(req.output_format.encoding),
        headers=headers,
    )


@router.post("/tts/stream")
async def tts_stream(req: TTSRequest, request: Request):
    """Stream synthesized/cached audio back in chunks (low TTFB on miss).

    HIT streams the cached blob; MISS streams provider chunks as they are
    synthesized and tees the full clip to the cache on completion.

    The first chunk is primed before the response commits so a provider
    connection failure maps to a proper 502/503 instead of a truncated 200.
    """
    cache = request.app.state.cache
    try:
        provider, _ = parse_model_id(req.model_id)
        headers, gen = await cache.stream(req)
    except ValueError as e:
        logger.warning(
            f"/tts/stream 400 — {e} | model_id={req.model_id!r} "
            f"transcript={req.transcript!r}"
        )
        raise HTTPException(status_code=400, detail=str(e))
    except ProviderNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ProviderBusy as e:
        raise HTTPException(status_code=503, detail=str(e), headers={"Retry-After": "1"})
    except ProviderError as e:
        raise HTTPException(
            status_code=502, detail=f"upstream {provider} returned an error: {e}"
        )

    try:
        first = await gen.__anext__()
    except StopAsyncIteration:
        first = b""
    except ProviderBusy as e:
        await gen.aclose()
        raise HTTPException(status_code=503, detail=str(e), headers={"Retry-After": "1"})
    except (ProviderError, httpx.HTTPStatusError, httpx.RequestError, OSError) as e:
        await gen.aclose()
        raise _map_upstream_error(provider, e)

    async def body(first_chunk: bytes) -> AsyncGenerator[bytes, None]:
        try:
            if first_chunk:
                yield first_chunk
            async for chunk in gen:
                yield chunk
        finally:
            # Release the provider generator promptly on client disconnect so
            # the underlying socket/cancel is cleaned up without waiting on GC.
            await gen.aclose()

    return StreamingResponse(
        body(first),
        media_type=content_type_for(req.output_format.encoding),
        headers=headers,
    )


@router.post("/tts/check", response_model=CheckResponse)
async def tts_check(req: TTSRequest, request: Request):
    cache = request.app.state.cache
    try:
        parse_model_id(req.model_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    cached, record, provider, model, key = await cache.check(req)
    return CheckResponse(
        cached=cached,
        key=key,
        provider=provider,
        voice_id=req.voice.id,
        model=model,
        encoding=record.encoding if record else None,
        sample_rate=record.sample_rate if record else None,
        size_bytes=record.size_bytes if record else None,
        hit_count=record.hit_count if record else None,
        created_at=record.created_at if record else None,
    )


@router.post("/tts/create", response_model=CreateResponse)
async def tts_create(req: TTSRequest, request: Request):
    cache = request.app.state.cache
    try:
        provider, _ = parse_model_id(req.model_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    audio_override = None
    if req.audio_base64:
        try:
            audio_override = base64.b64decode(req.audio_base64)
        except Exception:
            raise HTTPException(status_code=400, detail="invalid audio_base64")

    try:
        key, status, source, size, _, model, enc, rate = await cache.create(req, audio_override)
    except ProviderNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ProviderBusy as e:
        raise HTTPException(status_code=503, detail=str(e), headers={"Retry-After": "1"})
    except ProviderError as e:
        raise HTTPException(
            status_code=502, detail=f"upstream {provider} returned an error: {e}"
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        raise _map_upstream_error(provider, e)

    return CreateResponse(
        key=key,
        status=status,
        source=source,
        provider=provider,
        voice_id=req.voice.id,
        model=model,
        encoding=enc,
        sample_rate=rate,
        size_bytes=size,
    )


@router.post("/tts/delete", response_model=DeleteResponse)
async def tts_delete(req: TTSRequest, request: Request):
    cache = request.app.state.cache
    try:
        parse_model_id(req.model_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    deleted, key = await cache.delete(req)
    return DeleteResponse(status="deleted" if deleted else "not_found", key=key)


@router.post("/tts/create/bulk")
async def tts_create_bulk(requests: list[TTSRequest], request: Request):
    """Batch create/override many entries in one call (warm a phrase library).

    Each item honours the same rules as /tts/create. Per-item errors are
    collected (including base64/upstream failures) rather than aborting the batch.
    """
    cache = request.app.state.cache
    if len(requests) > settings.bulk_create_max:
        raise HTTPException(
            status_code=413,
            detail=f"bulk create capped at {settings.bulk_create_max} items per call",
        )
    results = []
    errors = []
    for i, req in enumerate(requests):
        try:
            parse_model_id(req.model_id)
            audio_override = None
            if req.audio_base64:
                audio_override = base64.b64decode(req.audio_base64)
            key, status, source, size, provider, model, _enc, _rate = await cache.create(req, audio_override)
            results.append(
                {
                    "index": i,
                    "key": key,
                    "status": status,
                    "source": source,
                    "provider": provider,
                    "size_bytes": size,
                }
            )
        except ValueError as e:
            errors.append({"index": i, "error": str(e)})
        except ProviderNotConfigured as e:
            errors.append({"index": i, "error": str(e)})
        except ProviderBusy:
            errors.append({"index": i, "error": "provider busy (bulkhead full); retry"})
        except ProviderError as e:
            errors.append({"index": i, "error": f"upstream error: {e}"})
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            errors.append({"index": i, "error": f"upstream error: {type(e).__name__}"})
    return {
        "created": len(results),
        "errors": len(errors),
        "results": results,
        "error_details": errors,
    }
