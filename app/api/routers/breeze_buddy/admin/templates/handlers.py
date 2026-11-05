"""Handler for ``DELETE /admin/templates/{id}/purge``."""

from __future__ import annotations

from fastapi import HTTPException, status

from app.ai.voice.agents.breeze_buddy.template.cache import invalidate_template
from app.core.logger import logger
from app.database.accessor.breeze_buddy.admin.template_purge import (
    fetch_template_purge_blockers,
    purge_template,
)
from app.database.accessor.breeze_buddy.template import get_template_by_id
from app.schemas import UserInfo
from app.schemas.breeze_buddy.admin.template_purge import (
    TemplatePurgeBlockers,
    TemplatePurgeResponse,
    TemplatePurgeTemplate,
)


def _blocker_reasons(blockers: TemplatePurgeBlockers) -> list[str]:
    reasons: list[str] = []
    if blockers.widget_configs:
        reasons.append(
            f"{blockers.widget_configs} widget config(s) still bind this template; "
            "rebind or delete the widget first"
        )
    if blockers.call_configs:
        reasons.append(
            f"{blockers.call_configs} call execution config(s) reference this template"
        )
    if blockers.inflight_leads:
        reasons.append(
            f"{blockers.inflight_leads} in-flight lead(s) (BACKLOG/RETRY/PROCESSING) "
            "use this template"
        )
    return reasons


async def purge_template_handler(
    template_id: str, current_user: UserInfo, *, dry_run: bool
) -> TemplatePurgeResponse:
    """Delete a template together with its chat history.

    Refuses (409) while a widget config, a call execution config or an
    in-flight lead still points at the template; those references are live
    behaviour, not history. Chat sessions are history and go with the
    template, which is the whole point of this endpoint over the tenant
    ``DELETE /templates/{id}``.
    """
    existing = await get_template_by_id(template_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Template not found: {template_id}",
        )

    try:
        counts = await fetch_template_purge_blockers(template_id)
    except Exception as exc:  # pragma: no cover - surfaced as 500
        logger.error(f"Purge pre-check failed for {template_id}: {exc}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not inspect the template's references",
        ) from exc

    blockers = TemplatePurgeBlockers(
        widget_configs=counts["widget_configs"],
        call_configs=counts["call_configs"],
        inflight_leads=counts["inflight_leads"],
    )
    reasons = _blocker_reasons(blockers)
    if reasons:
        detail = (
            f"Template '{existing.name}' cannot be purged. "
            f"Reasons: {'; '.join(reasons)}."
        )
        logger.warning(f"Template {template_id} purge blocked: {detail}")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)

    template = TemplatePurgeTemplate(
        id=template_id,
        name=existing.name,
        reseller_id=existing.reseller_id,
        merchant_id=existing.merchant_id,
        is_active=existing.is_active,
    )
    if dry_run:
        return TemplatePurgeResponse(
            purged=False,
            template=template,
            chat_sessions=counts["chat_sessions"],
            chat_messages=counts["chat_messages"],
            active_sessions=counts["active_sessions"],
            blockers=blockers,
        )

    logger.info(
        f"Admin {current_user.username} purging template {template_id} "
        f"('{existing.name}') with {counts['chat_sessions']} chat session(s) / "
        f"{counts['chat_messages']} message(s)"
    )
    try:
        result = await purge_template(template_id)
    except Exception as exc:
        logger.error(f"Template purge failed for {template_id}: {exc}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Purge failed and was rolled back: {exc}",
        ) from exc
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Template not found: {template_id}",
        )

    try:
        await invalidate_template(template_id)
    except Exception as cache_exc:  # pragma: no cover - best effort
        logger.warning(
            f"Template cache invalidation failed for {template_id}: {cache_exc}"
        )

    return TemplatePurgeResponse(
        purged=True,
        template=template,
        chat_sessions=result["chat_sessions_deleted"],
        chat_messages=counts["chat_messages"],
        active_sessions=counts["active_sessions"],
        blockers=blockers,
    )


__all__ = ["purge_template_handler"]
