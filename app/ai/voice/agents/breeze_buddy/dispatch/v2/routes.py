"""Template → number route, resolved on demand, plus number facts (design card §1, §6).

``bb:route:{T}`` is read from Redis every time (one HGETALL); there is no
per-process memo. A number's dispatch mode lives only in ``bb:num:{N}.mode``,
which this module reads but never writes (``switch.py`` owns it). A route expires
``keys.ROUTE_TTL_S`` after this module last wrote it; a number's hash never expires
(``keys.ROUTE_TTL_S`` says why).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple, cast

from app.ai.voice.agents.breeze_buddy.dispatch.v2 import keys as k
from app.core.config import dynamic as dyn_cfg
from app.core.logger import logger
from app.database.accessor import get_call_execution_config_by_template_id
from app.database.accessor.breeze_buddy.template import get_template_by_id
from app.services.redis import get_redis_service

# v2 calls on today's paths are bounded (rule 42): a hung Redis must not stall them.
TODAYS_PATH_TIMEOUT_S = 1.0
LEGACY = "legacy"
V2_ACCOUNTED_MODES = frozenset({"v2_pending", "v2", "draining"})


@dataclass(frozen=True)
class Route:
    template_id: str
    number_id: Optional[str]
    tier: str  # "high" | "medium" | "normal"
    start_sec: Optional[int]
    end_sec: Optional[int]
    enabled: bool
    reseller_id: str


def _sec(t) -> Optional[int]:
    return None if t is None else t.hour * 3600 + t.minute * 60 + t.second


async def _client() -> Any:
    """The raw Redis client, shared by the v2 jobs and hooks (Fable M7). Typed ``Any``: the
    service's ``Union[Redis, RedisCluster]`` hides the async signatures from pyrefly."""
    return cast(Any, await (await get_redis_service()).get_client())


async def _tiers() -> Tuple[set, set]:
    return (
        set(await dyn_cfg.BB_V2_TIER_HIGH_MERCHANT_IDS()),
        set(await dyn_cfg.BB_V2_TIER_MEDIUM_MERCHANT_IDS()),
    )


async def _available_number(config, template):
    # lazy: managers.calls imports the dispatch package (import cycle)
    from app.ai.voice.agents.breeze_buddy.managers.calls import _get_available_number

    return await _get_available_number(config, template)


async def resolve_route(template, config) -> Tuple[Route, object]:
    """Today's number rule applied to one template; returns the route and the number row."""
    number = await _available_number(config, template)
    high, medium = await _tiers()
    merchant = getattr(config, "merchant_id", None) or getattr(
        template, "merchant_id", None
    )
    tier = "high" if merchant in high else "medium" if merchant in medium else "normal"
    route = Route(
        template_id=str(template.id),
        number_id=str(number.id) if number is not None else None,
        tier=tier,
        start_sec=_sec(getattr(config, "call_start_time", None)),
        end_sec=_sec(getattr(config, "call_end_time", None)),
        enabled=bool(getattr(config, "enable_calling", True)),
        reseller_id=str(
            getattr(config, "reseller_id", "") or getattr(template, "reseller_id", "")
        ),
    )
    return route, number


async def ensure_route(
    template_id: str, *, template=None, config=None
) -> Optional[Route]:
    """Route from Redis, resolving and writing it on a miss. None if unresolvable or Redis fails."""
    try:
        client = await _client()
        h = await client.hgetall(k.route_key(template_id))
    except Exception as e:  # noqa: BLE001
        logger.error(f"v2 ensure_route read failed {template_id}: {e}")
        return None
    if h:
        return Route(
            template_id=template_id,
            number_id=h.get("number") or None,
            tier=h.get("tier", "normal"),
            start_sec=int(h["start"]) if h.get("start") else None,
            end_sec=int(h["end"]) if h.get("end") else None,
            enabled=h.get("enabled") == "1",
            reseller_id=h.get("reseller", ""),
        )
    return await _resolve_and_write(template_id, template, config)


async def _resolve_and_write(template_id: str, template, config) -> Optional[Route]:
    template = template or await get_template_by_id(template_id)
    config = config or await get_call_execution_config_by_template_id(template_id)
    if template is None or config is None:
        return None
    route, number = await resolve_route(template, config)
    await _write(route, number)
    return route


async def _write(route: Route, number) -> None:
    try:
        client = await _client()
        rk = k.route_key(route.template_id)
        old = await client.hget(rk, "number") or None
        async with client.pipeline(transaction=True) as pipe:
            pipe.hset(
                rk,
                mapping={
                    "number": route.number_id or "",
                    "tier": route.tier,
                    "start": "" if route.start_sec is None else route.start_sec,
                    "end": "" if route.end_sec is None else route.end_sec,
                    "enabled": "1" if route.enabled else "0",
                    "reseller": route.reseller_id,
                },
            )
            pipe.expire(rk, k.ROUTE_TTL_S)
            await pipe.execute()
        if old != route.number_id:
            if old:
                await client.srem(k.numtpl_key(old), route.template_id)
            if route.number_id:
                await client.sadd(k.numtpl_key(route.number_id), route.template_id)
        # Leads already waiting in the room: their number looks again now (a move must not
        # strand them on the new number; a route enabled again has no other timer).
        if route.number_id and await client.zcard(k.room_key(route.template_id)) > 0:
            now_ms = int(time.time() * 1000)
            await client.zadd(k.DUE_KEY, {route.number_id: now_ms}, lt=True)
    except Exception as e:  # noqa: BLE001
        logger.error(f"v2 route write failed {route.template_id}: {e}")
        return
    if number is not None:
        await refresh_number(number)


async def invalidate_route(template_id: str) -> None:
    """Re-resolve today's number rule for the template and rewrite its route."""
    try:
        await _resolve_and_write(template_id, None, None)
    except Exception as e:  # noqa: BLE001
        logger.error(f"v2 invalidate_route failed {template_id}: {e}")


async def refresh_number(number) -> None:
    """Write the number's facts (max, provider, status). Never writes ``mode``."""
    try:
        client = await _client()
        status = getattr(number.status, "value", str(number.status))
        provider = getattr(number.provider, "value", str(number.provider))
        # NULL maximum_channels counts as 1, like today's reconciler
        max_lines = (
            1 if number.maximum_channels is None else int(number.maximum_channels)
        )
        number_id = str(number.id)
        nk = k.num_key(number_id)
        async with client.pipeline(transaction=True) as pipe:
            pipe.hget(nk, "max")
            pipe.hset(
                nk, mapping={"max": max_lines, "provider": provider, "status": status}
            )
            # never a TTL (keys.ROUTE_TTL_S says why); clears one older code set
            pipe.persist(nk)
            old_max = (await pipe.execute())[0]
        if old_max is not None and int(old_max) < max_lines:
            # lines match has not seen yet: the number looks again now (a first write
            # needs none: no lead can wait on a number without a mode)
            now_ms = int(time.time() * 1000)
            await client.zadd(k.DUE_KEY, {number_id: now_ms}, lt=True)
    except Exception as e:  # noqa: BLE001
        logger.error(f"v2 refresh_number failed {getattr(number, 'id', None)}: {e}")


async def number_mode_or_none(number_id: str) -> Optional[str]:
    """``bb:num:{N}.mode`` ("legacy" when absent); None when the read failed, so callers
    can take a safe default instead of guessing (R-ERR)."""
    try:
        client = await _client()
        # bounded: a hung Redis must not stall today's DB-only paths (release, inbound)
        mode = await asyncio.wait_for(
            client.hget(k.num_key(number_id), "mode"), timeout=TODAYS_PATH_TIMEOUT_S
        )
        return mode or LEGACY
    except Exception as e:  # noqa: BLE001
        logger.error(f"v2 number_mode_or_none failed {number_id}: {e}")
        return None


async def epoch_present_or_none() -> Optional[bool]:
    """Is ``bb:epoch`` set, i.e. does Redis still hold v2's state? It goes missing when
    Redis restarts, fails over or is flushed, until the sweep leader's recovery sets it
    again. None when the read failed."""
    try:
        client = await _client()
        return bool(
            await asyncio.wait_for(
                client.exists(k.EPOCH_KEY), timeout=TODAYS_PATH_TIMEOUT_S
            )
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"v2 epoch read failed: {e}")
        return None


async def number_modes(number_ids: Sequence[str]) -> Dict[str, Optional[str]]:
    """``bb:num:{N}.mode`` of every number in one round trip (None where no mode is
    written). Raises on a Redis error."""
    if not number_ids:
        return {}
    client = await _client()
    async with client.pipeline(transaction=False) as pipe:
        for number_id in number_ids:
            pipe.hget(k.num_key(number_id), "mode")
        modes = await pipe.execute()
    return dict(zip(number_ids, modes))


async def route_number(template_id: str) -> Optional[str]:
    """``bb:route:{T}.number``: None if the template has no route yet. Raises on a Redis
    error, so a caller that must not guess (R-ERR) can tell "no route" from "unreadable".
    """
    client = await _client()
    return await client.hget(k.route_key(template_id), "number")


async def template_is_v2_accounted(template_id: Optional[str]) -> Optional[bool]:
    """True if the template's number is v2-accounted (its leads belong in v2 rooms), False
    if not (or the template has no number), None if that could not be read (R-ERR: the
    caller must not guess)."""
    if not template_id:
        return False
    try:
        number_id = await route_number(template_id)
        if number_id is None:  # no route yet: resolve it (None = unresolvable template)
            route = await ensure_route(template_id)
            number_id = route.number_id if route is not None else None
        if not number_id:
            return False
        mode = await number_mode_or_none(number_id)
        return None if mode is None else mode in V2_ACCOUNTED_MODES
    except Exception as e:  # noqa: BLE001
        logger.warning(f"v2 template mode check failed for {template_id}: {e}")
        return None
