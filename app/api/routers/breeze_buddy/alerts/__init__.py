"""
Alert fire endpoint.

POST /alerts/fire -- receives an alert event, deduplicates via Redis,
looks up alert group phone numbers, and pushes one BB lead per phone number.

This is the single interface for all alert sources:
- OpenObserve webhook destinations
- Internal HealthMonitor background task
- Future polling-based checks (e.g. balance pollers)

Security:
- reseller_id comes from the JWT token (not request body) to prevent impersonation.
- Token must be scoped to exactly one reseller (reseller_ids must have a single entry).
- merchant_id (optional) comes from the body, validated against token scope.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.routers.breeze_buddy.leads.rbac import validate_lead_access
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import AlertFireRequest, UserInfo, UserRole

from .handlers import fire_alert_handler

router = APIRouter()


def _resolve_reseller_id(current_user: UserInfo) -> str:
    """Extract the single reseller_id from the token.

    ALERT_SYSTEM tokens must be scoped to exactly one reseller.
    ADMIN tokens must also be scoped (admins should create a dedicated
    alert_system user per reseller rather than using their own token).

    Raises:
        HTTPException 403 if token has no reseller_ids, multiple, or wildcard.
    """
    ids = current_user.reseller_ids
    if not ids or len(ids) != 1 or ids[0] == "*":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Alert token must be scoped to exactly one reseller. "
                f"Got reseller_ids={ids}. "
                "Create a dedicated alert_system user with a single reseller_id."
            ),
        )
    return ids[0]


@router.post("/alerts/fire", status_code=status.HTTP_202_ACCEPTED)
async def fire_alert(
    req: AlertFireRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Fire a voice alert.

    Flow:
    1. Validate role (ADMIN or ALERT_SYSTEM only)
    2. Extract reseller_id from JWT token (must be single-scoped)
    3. Validate merchant_id (if provided) against token scope
    4. Deduplicate on alert_id via Redis SETNX with dedup_ttl_seconds TTL
    5. Look up alert_group_name in alert_groups table for this reseller
    6. Validate template + call execution config exist (fail fast)
    7. Push one BB lead per group member via create_lead_call_tracker()
    8. Existing process_backlog_leads() cron picks up leads and fires calls

    Auth: ADMIN or ALERT_SYSTEM JWT role required. Token must be scoped
    to exactly one reseller_id.
    """
    # Step 1: Role gate
    if current_user.role not in (UserRole.ADMIN, UserRole.ALERT_SYSTEM):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only ADMIN or ALERT_SYSTEM roles can fire alerts",
        )

    # Step 2: Extract reseller_id from token
    reseller_id = _resolve_reseller_id(current_user)

    # Step 3: Validate merchant_id scope (reuses existing leads RBAC)
    validate_lead_access(
        current_user, reseller_id, req.merchant_id, operation="fire alert"
    )

    return await fire_alert_handler(req, current_user, reseller_id)
