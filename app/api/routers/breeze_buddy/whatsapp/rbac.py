"""RBAC helpers for Breeze Buddy WhatsApp connection endpoints."""

from fastapi import HTTPException, status

from app.core.logger import logger
from app.schemas import UserInfo, UserRole


def _has_scope(scope: list[str], value: str) -> bool:
    """Return whether a token scope allows a requested value."""
    return "*" in scope or value in scope


def require_whatsapp_connection_access(
    current_user: UserInfo,
    *,
    reseller_id: str,
    merchant_id: str,
    operation: str,
) -> None:
    """Require admin access or exact reseller and merchant scope."""
    if current_user.role == UserRole.ADMIN:
        return

    if _has_scope(current_user.reseller_ids, reseller_id) and _has_scope(
        current_user.merchant_ids, merchant_id
    ):
        return

    logger.warning(
        f"User {current_user.username} denied WhatsApp {operation} access for "
        f"reseller={reseller_id} merchant={merchant_id}"
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Access denied to WhatsApp connection scope",
    )
