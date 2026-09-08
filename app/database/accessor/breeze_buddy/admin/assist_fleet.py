"""Async accessors for the assist-fleet read model.

Rows come back as plain dicts (the ``chat_analytics`` accessor idiom) — the
fleet builder in ``assist/fleet.py`` is the layer that shapes them. jsonb
columns are decoded here because asyncpg returns them as ``str`` unless a
codec is registered.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Sequence

from app.core.logger import logger
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.admin.assist_fleet import (
    all_widget_configs_query,
    blueprint_templates_query,
    chat_templates_query,
    merchant_session_stats_query,
    merchants_by_ids_query,
    template_daily_sessions_query,
    template_session_depth_query,
    template_session_stats_query,
    voice_template_counts_query,
)


def _jsonb(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value


def _rows(result) -> List[Dict[str, Any]]:
    return [dict(r) for r in result] if result else []


async def fetch_all_widget_configs() -> List[Dict[str, Any]]:
    try:
        query, values = all_widget_configs_query()
        rows = _rows(await run_parameterized_query(query, values))
        for r in rows:
            r["id"] = str(r["id"])
            r["template_id"] = str(r["template_id"])
            r["allowed_origins"] = list(r.get("allowed_origins") or [])
            r["appearance"] = _jsonb(r.get("appearance"), {})
        return rows
    except Exception as e:
        logger.error(f"assist-fleet: error listing widget configs: {e}")
        raise


async def fetch_chat_templates(
    reseller_ids: Sequence[str], template_ids: Sequence[str]
) -> List[Dict[str, Any]]:
    try:
        query, values = chat_templates_query(reseller_ids, template_ids)
        rows = _rows(await run_parameterized_query(query, values))
        for r in rows:
            r["id"] = str(r["id"])
            r["supported_channels"] = list(r.get("supported_channels") or [])
            r["flow"] = _jsonb(r.get("flow"), {})
            r["configurations"] = _jsonb(r.get("configurations"), {})
        return rows
    except Exception as e:
        logger.error(f"assist-fleet: error listing chat templates: {e}")
        raise


async def fetch_voice_template_counts(
    reseller_ids: Sequence[str],
) -> Dict[str, int]:
    """``"reseller|merchant" -> active voice-only template count``."""
    try:
        query, values = voice_template_counts_query(reseller_ids)
        rows = _rows(await run_parameterized_query(query, values))
        return {
            f"{r['reseller_id']}|{r['merchant_id']}": int(r["voice_templates"])
            for r in rows
            if r.get("merchant_id")
        }
    except Exception as e:
        logger.error(f"assist-fleet: error counting voice templates: {e}")
        raise


async def fetch_blueprint_templates(
    reseller_ids: Sequence[str], name: str
) -> List[Dict[str, Any]]:
    try:
        query, values = blueprint_templates_query(reseller_ids, name)
        rows = _rows(await run_parameterized_query(query, values))
        for r in rows:
            r["id"] = str(r["id"])
            r["flow"] = _jsonb(r.get("flow"), {})
        return rows
    except Exception as e:
        logger.error(f"assist-fleet: error loading blueprint templates: {e}")
        raise


async def fetch_merchants_by_ids(
    merchant_ids: Sequence[str],
) -> Dict[str, Dict[str, Any]]:
    """``"reseller|merchant" -> merchants row`` for the given merchant ids."""
    if not merchant_ids:
        return {}
    try:
        query, values = merchants_by_ids_query(merchant_ids)
        rows = _rows(await run_parameterized_query(query, values))
        return {f"{r['reseller_id']}|{r['merchant_id']}": r for r in rows}
    except Exception as e:
        logger.error(f"assist-fleet: error loading merchants: {e}")
        raise


async def fetch_template_session_stats(
    template_ids: Sequence[str], since_window: datetime, since_7d: datetime
) -> Dict[str, Dict[str, Any]]:
    if not template_ids:
        return {}
    try:
        query, values = template_session_stats_query(
            template_ids, since_window, since_7d
        )
        rows = _rows(await run_parameterized_query(query, values))
        return {r["template_id"]: r for r in rows}
    except Exception as e:
        logger.error(f"assist-fleet: error aggregating template sessions: {e}")
        raise


async def fetch_template_session_depth(
    template_ids: Sequence[str], since_window: datetime
) -> Dict[str, Dict[str, Any]]:
    if not template_ids:
        return {}
    try:
        query, values = template_session_depth_query(template_ids, since_window)
        rows = _rows(await run_parameterized_query(query, values))
        return {r["template_id"]: r for r in rows}
    except Exception as e:
        logger.error(f"assist-fleet: error aggregating session depth: {e}")
        raise


async def fetch_template_daily_sessions(
    template_ids: Sequence[str], since_window: datetime
) -> Dict[str, Dict[str, int]]:
    """``template_id -> {"YYYY-MM-DD": sessions}``."""
    if not template_ids:
        return {}
    try:
        query, values = template_daily_sessions_query(template_ids, since_window)
        rows = _rows(await run_parameterized_query(query, values))
        out: Dict[str, Dict[str, int]] = {}
        for r in rows:
            out.setdefault(r["template_id"], {})[r["day"]] = int(r["sessions"])
        return out
    except Exception as e:
        logger.error(f"assist-fleet: error bucketing daily sessions: {e}")
        raise


async def fetch_merchant_session_stats(
    reseller_ids: Sequence[str],
    merchant_ids: Sequence[str],
    since_window: datetime,
    since_7d: datetime,
) -> Dict[str, Dict[str, Any]]:
    if not merchant_ids:
        return {}
    try:
        query, values = merchant_session_stats_query(
            reseller_ids, merchant_ids, since_window, since_7d
        )
        rows = _rows(await run_parameterized_query(query, values))
        return {f"{r['reseller_id']}|{r['merchant_id']}": r for r in rows}
    except Exception as e:
        logger.error(f"assist-fleet: error aggregating merchant sessions: {e}")
        raise


__all__ = [
    "fetch_all_widget_configs",
    "fetch_blueprint_templates",
    "fetch_chat_templates",
    "fetch_merchant_session_stats",
    "fetch_merchants_by_ids",
    "fetch_template_daily_sessions",
    "fetch_template_session_depth",
    "fetch_template_session_stats",
    "fetch_voice_template_counts",
]
