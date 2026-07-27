"""Buddy Copilot data scope resolver."""

from __future__ import annotations

from datetime import datetime, timedelta
from inspect import isawaitable
from typing import Awaitable, Callable, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.security.scope import resolve_merchant_ids
from app.database.accessor.breeze_buddy.template import get_template_merchant_id
from app.schemas import UserInfo
from app.schemas.breeze_buddy.copilot import (
    DEFAULT_COPILOT_TIMEZONE,
    CopilotActorScope,
    CopilotCapability,
    CopilotDataScope,
    CopilotDateRangeSource,
    CopilotDateWindow,
    CopilotRequestedDateRange,
    CopilotScope,
    CopilotScopeRequest,
)

TemplateMerchantLoader = Callable[[str], Awaitable[Optional[str]] | Optional[str]]
MerchantScopeResolver = Callable[
    [UserInfo], Awaitable[Optional[List[str]]] | Optional[List[str]]
]

_PHASE_ONE_CAPABILITIES = (
    CopilotCapability.ANALYTICS_SUMMARY,
    CopilotCapability.QUERY_CONVERSATIONS,
    CopilotCapability.CONVERSATION_DETAIL,
)


class CopilotScopeError(Exception):
    """Fail-closed scope resolution error."""

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _actor_scope(current_user: UserInfo) -> CopilotActorScope:
    return CopilotActorScope(
        user_id=current_user.id,
        username=current_user.username,
        role=(
            current_user.role.value
            if hasattr(current_user.role, "value")
            else str(current_user.role)
        ),
        permissions=tuple(current_user.permissions),
        reseller_ids=tuple(current_user.reseller_ids),
        merchant_ids=tuple(current_user.merchant_ids),
    )


async def _resolve_allowed_merchant_ids(
    current_user: UserInfo,
    merchant_scope_resolver: MerchantScopeResolver,
) -> Optional[List[str]]:
    resolved = merchant_scope_resolver(current_user)
    if isawaitable(resolved):
        return await resolved
    return resolved


async def _resolve_data_merchant_id(
    request: CopilotScopeRequest,
    current_user: UserInfo,
    merchant_scope_resolver: MerchantScopeResolver,
) -> str:
    allowed_merchant_ids = await _resolve_allowed_merchant_ids(
        current_user, merchant_scope_resolver
    )

    requested = request.data_merchant_id
    if requested:
        if allowed_merchant_ids is None or requested in allowed_merchant_ids:
            return requested
        raise CopilotScopeError(
            "unauthorized_merchant",
            "Access denied to the selected Copilot data merchant.",
            status_code=403,
        )

    if allowed_merchant_ids is not None and len(allowed_merchant_ids) == 1:
        return allowed_merchant_ids[0]

    raise CopilotScopeError(
        "ambiguous_merchant",
        "A selected data_merchant_id is required for Buddy Copilot.",
        status_code=400,
    )


def _resolve_date_window(
    requested: Optional[CopilotRequestedDateRange],
    timezone: str,
    now: Optional[datetime],
) -> CopilotDateWindow:
    try:
        tzinfo = ZoneInfo(timezone or DEFAULT_COPILOT_TIMEZONE)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise CopilotScopeError(
            "invalid_timezone",
            "Buddy Copilot requires a valid IANA timezone.",
            status_code=400,
        ) from exc

    normalized_now = now or datetime.now(tzinfo)
    if normalized_now.tzinfo is None:
        normalized_now = normalized_now.replace(tzinfo=tzinfo)
    else:
        normalized_now = normalized_now.astimezone(tzinfo)

    if requested is not None:
        return CopilotDateWindow(
            timezone=timezone,
            date_from=requested.date_from,
            date_to=requested.date_to,
            source=CopilotDateRangeSource.REQUEST,
        )

    date_to = normalized_now.date()
    date_from = date_to - timedelta(days=6)
    return CopilotDateWindow(
        timezone=timezone or DEFAULT_COPILOT_TIMEZONE,
        date_from=date_from,
        date_to=date_to,
        source=CopilotDateRangeSource.DEFAULT,
    )


async def _load_template_merchant_id(
    template_merchant_loader: TemplateMerchantLoader,
    template_id: str,
) -> Optional[str]:
    merchant_id = template_merchant_loader(template_id)
    if isawaitable(merchant_id):
        return await merchant_id
    return merchant_id


async def _validate_data_template(
    data: CopilotDataScope,
    template_merchant_loader: TemplateMerchantLoader,
) -> None:
    if not data.data_template_id:
        return

    template_merchant_id = await _load_template_merchant_id(
        template_merchant_loader,
        data.data_template_id,
    )
    if template_merchant_id != data.data_merchant_id:
        raise CopilotScopeError(
            "unauthorized_template",
            "Selected Copilot data template was not found or does not belong "
            "to the selected merchant.",
            status_code=403,
        )


async def resolve_copilot_scope(
    request: CopilotScopeRequest,
    current_user: UserInfo,
    *,
    now: Optional[datetime] = None,
    template_merchant_loader: Optional[TemplateMerchantLoader] = None,
    merchant_scope_resolver: Optional[MerchantScopeResolver] = None,
) -> CopilotScope:
    """Resolve the immutable Buddy Copilot data scope.

    Dashboard-provided data scope is treated as a request, never authority.
    """
    data = CopilotDataScope(
        data_merchant_id=await _resolve_data_merchant_id(
            request,
            current_user,
            merchant_scope_resolver or resolve_merchant_ids,
        ),
        data_template_id=request.data_template_id,
    )
    await _validate_data_template(
        data,
        template_merchant_loader or get_template_merchant_id,
    )

    date_window = _resolve_date_window(request.date_range, request.timezone, now)

    return CopilotScope(
        actor=_actor_scope(current_user),
        data=data,
        date_window=date_window,
        capabilities=_PHASE_ONE_CAPABILITIES,
    )
