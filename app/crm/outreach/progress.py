"""progress — the overnight drain's progress line, per merchant.

Every CRM_DRAIN_ALERT_INTERVAL_SECONDS (default 60; 0 = off) the walker
pod that carries this loop reads, per merchant with cold work: cold calls
placed since the drain began (on the line + ended), still queued in the
dialler, and cold runs still waiting for room — then logs one line with
those fields and posts the same to Slack (SLACK_WEBHOOK_URL). When a
merchant's cold work reaches zero it says "drained" once and goes quiet
until cold work appears again.

The drain begins when the plan's window opens: the pile is what the window
held since it closed (21:00 to 09:00 on Flipkart's plans), and it starts
moving at the opening. So "placed" counts from the last opening of the
merchant's live plans' windows, on each window's own clock — no calendar
day, no setting. A plan without a window never holds a run overnight and
so never has a pile.

One replica carries it: set the interval on that pod only. A second loop
on a second pod posts a second copy of the same numbers, nothing worse.
Neither Slack nor a failed read ever touches the walker's claim: this loop
is a reader.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import aiohttp

from app.core.config.static import CRM_DRAIN_ALERT_INTERVAL_SECONDS, SLACK_WEBHOOK_URL
from app.core.logger import logger
from app.crm.outreach.db.accessors import (
    enrollment as enrollment_accessor,
    workflow as workflow_accessor,
)
from app.crm.outreach.schemas import WaitWindow
from app.crm.outreach.window import last_opening
from app.database.accessor import cold_calls_since

LOG_COMPONENT = "crm.outreach.drain"
_SLACK_TIMEOUT_SECONDS = 10
# A merchant whose cold work comes from a plan that no longer carries a
# window (edited since the pile formed): count one day back.
_NO_WINDOW_LOOKBACK = timedelta(days=1)


def drain_start(now: datetime, windows: List[WaitWindow]) -> datetime:
    """PURE: when this merchant's drain began — the earliest of its plans'
    last window openings, so no plan's pile is left out."""
    if not windows:
        return now - _NO_WINDOW_LOOKBACK
    return min(last_opening(now, window) for window in windows)


def plan_windows(definitions: List[Any]) -> List[WaitWindow]:
    """PURE: every calling window the merchant's live plans set."""
    windows: List[WaitWindow] = []
    for definition in definitions:
        nodes = definition.get("nodes", []) if isinstance(definition, dict) else []
        for node in nodes:
            if isinstance(node, dict) and isinstance(node.get("window"), dict):
                try:
                    windows.append(WaitWindow.model_validate(node["window"]))
                except ValueError:
                    continue  # a malformed window: publish refuses it; skip
    return windows


def progress_line(
    merchant_id: str, waiting: int, calls: Dict[str, int]
) -> Dict[str, Any]:
    """PURE: one merchant's progress — what is logged and what Slack shows.
    `left` is the pile still to feed plus the cold calls the dialler has
    not placed; `placed` is every cold call that reached a line this
    drain."""
    queued = calls.get("queued", 0)
    on_line = calls.get("on_line", 0)
    ended = calls.get("ended", 0)
    left = waiting + queued
    return {
        "merchant_id": merchant_id,
        "cold_runs_waiting": waiting,
        "cold_calls_queued": queued,
        "cold_calls_on_line": on_line,
        "cold_calls_ended": ended,
        "cold_calls_placed": on_line + ended,
        "left": left,
        "drained": left == 0,
    }


def slack_text(line: Dict[str, Any]) -> str:
    if line["drained"]:
        return (
            f"Overnight drain — {line['merchant_id']}: DRAINED. "
            f"{line['cold_calls_placed']} cold calls placed this drain."
        )
    return (
        f"Overnight drain — {line['merchant_id']}: "
        f"{line['cold_calls_placed']} cold calls placed this drain "
        f"({line['cold_calls_on_line']} on the line now), "
        f"{line['cold_calls_queued']} queued in the dialler, "
        f"{line['cold_runs_waiting']} cold runs still waiting — "
        f"{line['left']} left."
    )


async def drain_progress_loop(stop_event: asyncio.Event) -> None:
    """The walker role's second loop (worker_main). Returns at once when
    the interval is 0."""
    interval = CRM_DRAIN_ALERT_INTERVAL_SECONDS
    if interval <= 0:
        return
    last_left: Dict[str, int] = {}
    while not stop_event.is_set():
        try:
            await _tick(last_left)
        except Exception as e:  # a reader: never let it end the loop
            logger.bind(component=LOG_COMPONENT).error(
                f"drain progress tick failed: {e}"
            )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def _tick(last_left: Dict[str, int]) -> None:
    now = datetime.now(timezone.utc)
    waiting = await enrollment_accessor.cold_pile_by_merchant()
    # Merchants with a pile now, and those we last saw with one — so the
    # "drained" line is said once after the pile is gone.
    with_pile = {m for m, n in waiting.items() if n}
    seen_with_pile = {m for m, left in last_left.items() if left}
    for merchant_id in sorted(with_pile | seen_with_pile):
        plans = await workflow_accessor.live_workflows(merchant_id)
        since = drain_start(now, plan_windows([w.definition for w in plans]))
        calls = await cold_calls_since(merchant_id, since)
        line = progress_line(merchant_id, waiting.get(merchant_id, 0), calls)
        last_left[merchant_id] = line["left"]
        logger.bind(
            component=LOG_COMPONENT, drain_since=since.isoformat(), **line
        ).info(slack_text(line))
        await _post(slack_text(line))


async def _post(text: str) -> None:
    if not SLACK_WEBHOOK_URL:
        return
    try:
        timeout = aiohttp.ClientTimeout(total=_SLACK_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(SLACK_WEBHOOK_URL, json={"text": text}) as resp:
                if resp.status >= 300:
                    logger.bind(component=LOG_COMPONENT).warning(
                        f"drain progress: Slack answered {resp.status}"
                    )
    except Exception as e:
        logger.bind(component=LOG_COMPONENT).warning(
            f"drain progress: Slack post failed: {e}"
        )
