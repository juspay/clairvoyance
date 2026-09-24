"""Website research: the older single-call scrape, and the researcher.

``/scraping/website`` asks one question in one call and gets prose back. It
onboards every live merchant today and is not going anywhere until the
onboarding pipeline itself moves.

``/assist/research`` runs a loop: it reads, looks at what came back, changes
tactic when a site will not give up its words, and writes down facts with the
address each was read on. That last property is the point — a wrong fact in an
assistant's prompt can be traced to the page that said it.
"""

import asyncio
import json
from typing import Any, AsyncIterator, Dict, Mapping, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.ai.voice.agents.breeze_buddy.assist import profiles
from app.ai.voice.agents.breeze_buddy.assist.engine.classify import classify_profile
from app.ai.voice.agents.breeze_buddy.assist.engine.probe import probe_site
from app.ai.voice.agents.breeze_buddy.assist.engine.research import agent, slots
from app.ai.voice.agents.breeze_buddy.assist.engine.research.exceptions import (
    WebsiteScrapingConfigurationError,
    WebsiteScrapingUpstreamError,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.research.website import (
    scrape_website,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.web.fetch import (
    EgressNotGuardedError,
    FetchFailedError,
    UnsafeUrlError,
)
from app.ai.voice.agents.breeze_buddy.assist.platforms import registry
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
    ResearchNote,
    SiteResearchRequest,
    SiteResearchResponse,
    SiteSlotsOut,
    SlotFieldOut,
    SlotSectionOut,
    WebsiteScrapingRequest,
    WebsiteScrapingResponse,
    WebsiteScrapingResult,
)
from app.services.redis.rate_limit import check_rate_limit

router = APIRouter()


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


__all__ = ["router"]


_RESEARCH_ROLES = [UserRole.ADMIN, UserRole.RESELLER]
# Much lower than the probe's cap: a run is tens of reads and a dozen model
# calls, so this is the most expensive thing on the assist surface.
_RESEARCH_LIMIT_PER_HOUR = 20
_RESEARCH_WINDOW_SECONDS = 3600


@router.post("/assist/research", response_model=SiteResearchResponse)
async def research_site(
    body: SiteResearchRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> SiteResearchResponse:
    """Read a brand's site until there is enough to build its assistant.

    Slow on purpose — tens of reads and a dozen model calls — so a caller
    should treat this as a stage with a progress bar, never as something a
    person waits on behind a spinner.
    """
    require_role(current_user, _RESEARCH_ROLES)
    validate_reseller_access(current_user, reseller_id=body.reseller_id)
    if body.merchant_id:
        validate_merchant_access(current_user, merchant_id=body.merchant_id)

    decision = await check_rate_limit(
        bucket="assist_research",
        identifier=current_user.id,
        limit=_RESEARCH_LIMIT_PER_HOUR,
        window_seconds=_RESEARCH_WINDOW_SECONDS,
        prefix="assist",
        fail_closed=True,
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Research rate limit hit ({decision.count}/{decision.limit} per "
                "hour). Try again later."
            ),
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )

    try:
        return await _run_research(body)
    except (UnsafeUrlError, FetchFailedError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except EgressNotGuardedError as exc:
        logger.error(f"assist research unavailable: {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except WebsiteScrapingConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


async def _run_research(
    body: SiteResearchRequest, progress: Optional[agent.Progress] = None
) -> SiteResearchResponse:
    """Recognise, gather, research, sort. The whole of it, once."""
    budget = agent.Budget()
    if body.max_steps:
        budget.steps = body.max_steps
    if body.max_reads:
        budget.fetches = body.max_reads

    # Recognise the site first, and let whichever adapter owns it hand over
    # whatever it already knows. On a hosted store that is the policies, text
    # and all, for one request — so the loop starts from them instead of
    # spending half its budget looking for them. On a site no adapter claims,
    # this is simply empty and the loop reads.
    site = await probe_site(body.url)
    adapter = registry.resolve(classify_profile(site).adapter_id)
    supplied = await adapter.known_documents(site)
    seeds = [
        agent.Seed(
            kind=document.kind,
            title=document.title,
            url=document.display_url or document.url,
            text=document.body,
        )
        for document in supplied
        if document.body
    ]
    outcome = await agent.research(
        body.url,
        guidance=body.guidance or "",
        seeds=seeds,
        budget=budget,
        progress=progress,
    )
    # Loose notes are the right shape for gathering and the wrong shape for
    # everything after. Which sections they land in is the adapter's call,
    # resolved outside the engine.
    profile = profiles.resolve(adapter.slot_profile())
    filled = await slots.fill(outcome.evidence.notes, profile)

    logger.info(
        "assist research done",
        steps=outcome.steps_used,
        notes=len(outcome.evidence.notes),
        documents=len(outcome.evidence.readable()),
        stopped=outcome.stopped_because,
    )
    return SiteResearchResponse(
        url=outcome.evidence.root,
        model=outcome.model,
        summary=outcome.summary,
        notes=[
            ResearchNote(
                field=note.field_name,
                value=note.value,
                source_url=note.source_url,
                on_site=note.on_site,
                noted_at=note.noted_at,
            )
            for note in outcome.evidence.notes
        ],
        trace=outcome.evidence.steps,
        steps_used=outcome.steps_used,
        documents_read=len(outcome.evidence.readable()),
        stopped_because=outcome.stopped_because,
        platform=adapter.id,
        slots=_slots_out(filled),
        context=slots.render(filled, profile),
        fields=_with_known(slots.as_fields(filled), adapter.known_fields(site)),
    )


def _with_known(found: Dict[str, Any], known: Mapping[str, str]) -> Dict[str, Any]:
    """Platform-known values, only where the reading came back empty.

    Never overwrites: a merchant who publishes a different basket address than
    the platform default means it, and the page they published it on is better
    evidence than our knowledge of how the platform is usually laid out.
    """
    for key, value in known.items():
        if value and not found.get(key):
            found[key] = [value]
    return found


def _slots_out(filled: slots.FilledSlots) -> SiteSlotsOut:
    return SiteSlotsOut(
        profile=filled.profile,
        sections=[
            SlotSectionOut(
                key=section.key,
                title=section.title,
                fields=[
                    SlotFieldOut(
                        key=entry.key,
                        label=entry.label,
                        values=entry.values,
                        sources=entry.sources,
                    )
                    for entry in section.fields
                ],
            )
            for section in filled.sections
        ],
        unplaced=filled.unplaced,
        filled_count=filled.filled_count,
    )


async def _research_events(body: SiteResearchRequest) -> AsyncIterator[str]:
    """Progress while it works, then the whole result.

    A run is a minute long. A screen that says nothing for a minute has failed
    no matter how good its eventual answer is, so the loop reports after every
    round and the queue below carries those out while the work continues.
    """
    queue: "asyncio.Queue[Optional[Dict[str, Any]]]" = asyncio.Queue()

    async def report(update: Dict[str, Any]) -> None:
        await queue.put({"step": "researching", "status": "running", **update})

    async def run() -> None:
        try:
            answer = await _run_research(body, progress=report)
            await queue.put(
                {
                    "step": "researching",
                    "status": "done",
                    "result": json.loads(answer.model_dump_json()),
                }
            )
        except (UnsafeUrlError, FetchFailedError) as exc:
            await queue.put(
                {"success": False, "code": "unreadable_site", "message": str(exc)}
            )
        except (EgressNotGuardedError, WebsiteScrapingConfigurationError) as exc:
            await queue.put(
                {"success": False, "code": "unavailable", "message": str(exc)}
            )
        except Exception as exc:  # noqa: BLE001 - the stream reports, never hangs
            logger.error(f"assist research stream failed: {exc}")
            await queue.put(
                {
                    "success": False,
                    "code": "research_failed",
                    "message": "Research could not be completed.",
                }
            )
        finally:
            await queue.put(None)

    worker = asyncio.create_task(run())
    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            # `format_sse` renders an SSEEvent, not a dict — the onboarding
            # stream hands it one already built. Named "message" so a
            # plain `onmessage` reader sees every event.
            yield format_sse(SSEEvent(event="message", data=event))
    finally:
        # A browser that navigates away should stop the work, not leave a
        # researcher reading someone's website into an empty room.
        if not worker.done():
            worker.cancel()


@router.post("/assist/research/stream")
async def research_site_stream(
    body: SiteResearchRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> StreamingResponse:
    """The same research, reported as it happens."""
    require_role(current_user, _RESEARCH_ROLES)
    validate_reseller_access(current_user, reseller_id=body.reseller_id)
    if body.merchant_id:
        validate_merchant_access(current_user, merchant_id=body.merchant_id)

    decision = await check_rate_limit(
        bucket="assist_research",
        identifier=current_user.id,
        limit=_RESEARCH_LIMIT_PER_HOUR,
        window_seconds=_RESEARCH_WINDOW_SECONDS,
        prefix="assist",
        fail_closed=True,
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Research rate limit hit ({decision.count}/{decision.limit} per "
                "hour). Try again later."
            ),
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )

    return StreamingResponse(
        _research_events(body),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
