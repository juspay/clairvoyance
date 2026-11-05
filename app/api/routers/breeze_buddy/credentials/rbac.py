"""RBAC for credential WRITES: who may create or edit a row, by role and scope.

A credential row decides where a template's conversations are sent (its
`provider` account carries the key and the host — see
docs/PROVIDER_CREDENTIALS.md), so whoever may edit a row controls that. The
route asks this before the handler: reads stay tenant-scoped as before, and
writes need the role that owns the row's scope.
"""

from typing import Optional

from fastapi import HTTPException, status

from app.core.logger import logger
from app.schemas import UserInfo


def require_credential_write_scope(
    current_user: UserInfo,
    reseller_id: Optional[str],
    merchant_id: Optional[str],
    operation: str = "write",
) -> None:
    """Refuse (403) unless the caller may write a row of this scope.

    - global row (no reseller): admin only
    - reseller-wide row (reseller, no merchant): admin, or a reseller-level
      role holding that reseller
    - merchant row: admin, or a caller holding that reseller AND that
      merchant (`merchant_ids`, or `*`)

    A merchant-level caller can therefore never touch a reseller-wide row,
    nor another merchant's row, whatever its reseller access says.
    """
    if current_user.role == "admin":
        return
    if reseller_id is None:
        _refuse(current_user, operation, "Only admin users can %s global credentials")
    if (
        reseller_id not in current_user.reseller_ids
        and "*" not in current_user.reseller_ids
    ):
        _refuse(current_user, operation, f"Access denied to reseller {reseller_id}")
    if merchant_id:
        if (
            merchant_id not in current_user.merchant_ids
            and "*" not in current_user.merchant_ids
        ):
            _refuse(current_user, operation, f"Access denied to merchant {merchant_id}")
        return
    if current_user.role != "reseller":
        _refuse(
            current_user,
            operation,
            "Reseller access required to %s reseller-wide credentials",
        )


def _refuse(current_user: UserInfo, operation: str, detail: str) -> None:
    message = detail % operation if "%s" in detail else detail
    logger.warning(
        f"User {current_user.username} (role: {current_user.role}) attempted to "
        f"{operation} a credential outside their scope: {message}"
    )
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=message)
