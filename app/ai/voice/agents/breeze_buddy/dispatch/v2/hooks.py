"""Save hooks: keep v2 routes and number facts in step with edits (design card §6 rules 2, 13).

Hooks refresh only what v2 reads (a template's route, a number's facts). They never write
a number's mode and never seed its busy list: switching is ``switch.py``'s job, driven by
the dynamic config. Every hook is a no-op until v2 is first used and never raises into the
handler that calls it. The sweep's route refresh (every BB_V2_ROUTES_REFRESH_S, 600 s by
default) and 5 s number-facts refresh catch any edit made without a hook.
"""

from __future__ import annotations

from typing import Any, Optional

from app.ai.voice.agents.breeze_buddy.dispatch.v2 import keys as k
from app.ai.voice.agents.breeze_buddy.dispatch.v2.latch import v2_seen
from app.ai.voice.agents.breeze_buddy.dispatch.v2.routes import (
    _client,
    invalidate_route,
    refresh_number,
    route_number,
)
from app.core.concurrency import spawn_background_task
from app.core.logger import logger


async def on_template_saved(template_id: Optional[str]) -> None:
    """A template or its call config was saved (number pin, hours, calling on/off): its
    route is re-resolved. A template with no route needs nothing; its first lead
    resolves one."""
    if not template_id:
        return
    try:
        if not await v2_seen():
            return
        if await route_number(str(template_id)) is not None:
            await invalidate_route(str(template_id))
    except Exception as e:  # noqa: BLE001 — never raise into the handler
        logger.error(f"v2 on_template_saved failed for {template_id}: {e}")


on_config_saved = on_template_saved


async def on_number_saved(number: Any) -> None:
    """A number was created, bought, edited or disabled: its facts (max, status, provider)
    are rewritten now, and the routes it may change are re-resolved in the background.
    """
    if number is None:
        return
    try:
        if not await v2_seen():
            return
        await refresh_number(number)
        provider = getattr(number.provider, "value", str(number.provider))
        spawn_background_task(
            reresolve_routes_on_provider(provider),
            name=f"bb-v2-reresolve-routes-{number.id}",
        )
    except Exception as e:  # noqa: BLE001 — never raise into the handler
        logger.error(
            f"v2 on_number_saved failed for {getattr(number, 'id', None)}: {e}"
        )


async def reresolve_routes_on_provider(provider: str) -> None:
    """Today's fallback number rule depends on every number of a provider (rule 2), so a
    number change can move unpinned templates. Re-resolve the routes on v2-accounted
    numbers of that provider; re-resolving a pinned template is harmless. A template
    whose route points at today's path heals when a worker hands one of its leads back
    to v2 (``queue.requeue_in_room``)."""
    try:
        c = await _client()
        for number_id in sorted(await c.smembers(k.V2_ACTIVE_KEY)):
            if await c.hget(k.num_key(number_id), "provider") != provider:
                continue
            for template_id in sorted(await c.smembers(k.numtpl_key(number_id))):
                await invalidate_route(template_id)
    except Exception as e:  # noqa: BLE001
        logger.error(f"v2 route re-resolve for {provider} failed: {e}")
