"""Handler for ``GET /admin/assist-fleet``.

Loads the raw rows (nine read-only queries, the independent ones in parallel;
two gathers because typeshed's ``gather`` overloads stop at six positionals),
then hands everything to the pure ``build_fleet``. The route module owns auth;
this module owns orchestration; ``assist/fleet.py`` owns every decision.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, status

from app.ai.voice.agents.breeze_buddy.assist.commerce.assist_onboarding import (
    DEFAULT_ASSIST_TEMPLATE_NAME,
)
from app.ai.voice.agents.breeze_buddy.assist.fleet import (
    ASSIST_RESELLERS,
    FleetInputs,
    build_fleet,
)
from app.core.logger import logger
from app.database.accessor.breeze_buddy.admin.assist_fleet import (
    fetch_all_widget_configs,
    fetch_blueprint_templates,
    fetch_chat_templates,
    fetch_merchant_session_stats,
    fetch_merchants_by_ids,
    fetch_template_daily_sessions,
    fetch_template_session_depth,
    fetch_template_session_stats,
    fetch_voice_template_counts,
)
from app.schemas import UserInfo
from app.schemas.breeze_buddy.admin.assist_fleet import AssistFleetResponse


async def get_assist_fleet_handler(
    current_user: UserInfo,
    *,
    window_days: int = 30,
    reference_template_id: Optional[str] = None,
) -> AssistFleetResponse:
    now = datetime.now(timezone.utc)
    since_window = now - timedelta(days=window_days)
    since_7d = now - timedelta(days=7)
    logger.info(
        f"assist-fleet: {current_user.username} requested the fleet "
        f"(window_days={window_days}, reference={reference_template_id})"
    )
    try:
        widgets = await fetch_all_widget_configs()
        bound_ids = sorted({w["template_id"] for w in widgets})
        extra_ids = list(bound_ids)
        if reference_template_id and reference_template_id not in extra_ids:
            extra_ids.append(reference_template_id)
        templates = await fetch_chat_templates(ASSIST_RESELLERS, extra_ids)
        template_ids = [t["id"] for t in templates]
        merchant_ids = sorted({w["merchant_id"] for w in widgets})
        reseller_ids = sorted({w["reseller_id"] for w in widgets})

        merchants, voice_counts, blueprints = await asyncio.gather(
            fetch_merchants_by_ids(merchant_ids),
            fetch_voice_template_counts(ASSIST_RESELLERS),
            fetch_blueprint_templates(ASSIST_RESELLERS, DEFAULT_ASSIST_TEMPLATE_NAME),
        )
        (
            template_stats,
            template_depth,
            template_daily,
            merchant_stats,
        ) = await asyncio.gather(
            fetch_template_session_stats(template_ids, since_window, since_7d),
            fetch_template_session_depth(template_ids, since_window),
            fetch_template_daily_sessions(template_ids, since_window),
            fetch_merchant_session_stats(
                reseller_ids, merchant_ids, since_window, since_7d
            ),
        )
    except Exception as e:  # accessors already logged the SQL failure
        logger.error(f"assist-fleet: failed to load fleet rows: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not load the assist fleet.",
        ) from e

    inputs = FleetInputs(
        widgets=widgets,
        templates=templates,
        merchants=merchants,
        voice_counts=voice_counts,
        blueprints=blueprints,
        template_stats=template_stats,
        template_depth=template_depth,
        template_daily=template_daily,
        merchant_stats=merchant_stats,
        reference_template_id=reference_template_id,
    )
    return build_fleet(inputs, now=now, window_days=window_days)


__all__ = ["get_assist_fleet_handler"]
