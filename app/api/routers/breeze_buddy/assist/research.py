"""Website scraping, and ``POST /assist/research/stream``: read a store's site
and stream back the facts found, each with the page it came from."""

import contextlib
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.ai.voice.agents.breeze_buddy.assist.engine.research import runs
from app.ai.voice.agents.breeze_buddy.assist.engine.research.exceptions import (
    WebsiteScrapingConfigurationError,
    WebsiteScrapingUpstreamError,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.research.website import (
    scrape_website,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.web.fetch import (
    UnsafeUrlError,
    normalize_probe_url,
)
from app.ai.voice.agents.breeze_buddy.chat.sse import SSEEvent, format_sse
from app.api.security.breeze_buddy.authorization import (
    validate_merchant_access,
    validate_reseller_access,
)
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.logger import logger
from app.core.security.authorization import require_role
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.assist.research import (
    SiteResearchRequest,
    WebsiteScrapingRequest,
    WebsiteScrapingResponse,
    WebsiteScrapingResult,
)
from app.services.redis.rate_limit import check_rate_limit

router = APIRouter()

_RESEARCH_ROLES = [UserRole.ADMIN, UserRole.RESELLER, UserRole.MERCHANT]
# A run is tens of page reads and up to 16 model calls: the costliest call on
# the assist surface, so it is capped per merchant per day.
_RUNS_PER_MERCHANT_PER_DAY = 3
# Counted on the caller as well, since ids in the body are the caller's choice.
_RUNS_PER_USER_PER_DAY = 10
_RESEARCH_WINDOW_SECONDS = 24 * 3600
# Runs at once on this worker, and per caller: each can hold 32M characters of
# page text and its text tools hold the GIL, next to live calls. The daily
# counts above do not stop a caller from starting all their runs together.
_slots = runs.RunSlots(total=4, per_user=2)
_BUSY_RETRY_SECONDS = 30


@router.post(
    "/scraping/website",
    response_model=WebsiteScrapingResponse,
)
async def scrape_website_endpoint(
    body: WebsiteScrapingRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> WebsiteScrapingResponse:
    """Scrape website context using the requested service."""
    # Website scraping invokes a paid Gemini generation. Tenant scope checks
    # below determine *which* data a caller may access; this role gate controls
    # who may spend platform quota in the first place.
    require_role(current_user, [UserRole.ADMIN, UserRole.RESELLER])

    try:
        validate_reseller_access(current_user, reseller_id=body.reseller_id)
        if body.merchant_id:
            validate_merchant_access(current_user, merchant_id=body.merchant_id)

        result = await scrape_website(
            provider=body.provider,
            provider_config=body.provider_config,
            url=body.url,
            timeout_seconds=body.timeout_seconds,
        )
    except WebsiteScrapingConfigurationError as exc:
        logger.error("Website scraping provider is not configured", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Website scraping service is unavailable",
        ) from exc
    except WebsiteScrapingUpstreamError as exc:
        logger.warning("Website scraping provider returned no usable content")
        raise HTTPException(
            status_code=502,
            detail="Website scraping provider returned an invalid response",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Website scraping service failed", exc_info=True)
        raise HTTPException(
            status_code=502,
            detail="Website scraping service failed",
        ) from exc

    return WebsiteScrapingResponse(
        provider=body.provider,
        result=WebsiteScrapingResult(
            text=result.text,
            status=result.status,
            url_context_metadata=result.url_context_metadata,
        ),
        provider_response=result.provider_response,
    )


@router.post("/assist/research/stream")
async def research_site_stream(
    body: SiteResearchRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> StreamingResponse:
    """Read the site at ``body.url`` and stream what was found.

    Events: ``progress`` {step, status, detail}, ``note`` {field, value,
    source_url}, then exactly one of ``done`` {notes, pages_read} or
    ``error`` {code, message, retryable}.
    """
    require_role(current_user, _RESEARCH_ROLES)
    validate_reseller_access(current_user, reseller_id=body.reseller_id)
    if current_user.role == UserRole.MERCHANT and not body.merchant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="merchant_id is required"
        )
    if body.merchant_id:
        validate_merchant_access(current_user, merchant_id=body.merchant_id)
    try:
        url = normalize_probe_url(body.url)
    except UnsafeUrlError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    # Claimed before the daily counts, so a busy refusal does not spend a run.
    try:
        slot = _slots.claim(current_user.id)
    except runs.RunsBusyError as exc:
        raise HTTPException(
            status_code=(
                status.HTTP_429_TOO_MANY_REQUESTS
                if exc.scope == "user"
                else status.HTTP_503_SERVICE_UNAVAILABLE
            ),
            detail="Research is busy right now. Try again in a minute.",
            headers={"Retry-After": str(_BUSY_RETRY_SECONDS)},
        ) from exc
    try:
        await _count_run(current_user.id, body.reseller_id, body.merchant_id)
    except BaseException:
        slot.release()
        raise

    return _RunResponse(
        _sse(runs.research_events(url)),
        slot,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


async def _count_run(
    user_id: str, reseller_id: str, merchant_id: Optional[str]
) -> None:
    """Spend one of today's runs for the merchant and the caller, or raise 429.

    Each check counts before it decides, so the merchant goes first: a merchant
    already at its cap must not use up the caller's own runs on every retry.
    """
    limits = []
    if merchant_id:
        limits.append(
            (
                "assist_research_merchant",
                f"{reseller_id}:{merchant_id}",
                _RUNS_PER_MERCHANT_PER_DAY,
            )
        )
    limits.append(("assist_research_user", user_id, _RUNS_PER_USER_PER_DAY))
    for bucket, identifier, limit in limits:
        decision = await check_rate_limit(
            bucket=bucket,
            identifier=identifier,
            limit=limit,
            window_seconds=_RESEARCH_WINDOW_SECONDS,
            prefix="assist",
            fail_closed=True,
        )
        if not decision.allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Research limit reached ({decision.limit} runs a day). "
                    "Try again tomorrow."
                ),
                headers={"Retry-After": str(decision.retry_after_seconds)},
            )


async def _sse(events: AsyncGenerator[runs.Event, None]) -> AsyncGenerator[str, None]:
    async with contextlib.aclosing(events):
        async for event, data in events:
            yield format_sse(SSEEvent(event=event, data=data))


class _RunResponse(StreamingResponse):
    """Frees the run's slot however the response ends, even if it never
    streams. Background tasks are not enough: Starlette skips them when the
    client disconnects."""

    def __init__(
        self, content: AsyncGenerator[str, None], slot: runs.Slot, **kwargs
    ) -> None:
        super().__init__(content, **kwargs)
        self._events = content
        self._slot = slot

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                # A send that failed mid-stream leaves the stream open: close
                # it so the run stops too.
                await self._events.aclose()
            finally:
                self._slot.release()


__all__ = ["router"]
