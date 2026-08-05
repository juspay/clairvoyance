"""Provider-neutral website scraping endpoint."""

from fastapi import APIRouter, Depends, HTTPException

from app.api.security.breeze_buddy.authorization import (
    validate_merchant_access,
    validate_reseller_access,
)
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.logger import logger
from app.core.security.authorization import require_role
from app.schemas import UserInfo, UserRole
from app.schemas.scraper.website import (
    WebsiteScrapingRequest,
    WebsiteScrapingResponse,
    WebsiteScrapingResult,
)
from app.services.scraper.website.exceptions import (
    WebsiteScrapingConfigurationError,
    WebsiteScrapingUpstreamError,
)
from app.services.scraper.website.scraper import scrape_website

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
