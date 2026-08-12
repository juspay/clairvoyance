"""
Event-driven dispatch module for Breeze Buddy.

Replaces the cron-based `process_backlog_leads` with a Redis-backed
ZSET-as-schedule + leader-elected promoter + worker-pool design.

See docs/BACKLOG_DISPATCHER_REDESIGN.md for the architecture.
"""

from typing import Optional

from app.ai.voice.agents.breeze_buddy.dispatch.channel_semaphore import (
    acquire_channel_token,
    init_channel_semaphore,
    release_channel_token,
)
from app.ai.voice.agents.breeze_buddy.dispatch.leader import LeaderElection
from app.ai.voice.agents.breeze_buddy.dispatch.promoter import (
    start_promoter,
    stop_promoter,
)
from app.ai.voice.agents.breeze_buddy.dispatch.queue import (
    DISPATCHABLE_EXECUTION_MODES,
    cancel_scheduled_lead,
    is_dispatchable,
    schedule_lead,
)
from app.ai.voice.agents.breeze_buddy.dispatch.reconcilers import (
    clean_stale_bb_locks,
    monitor_dispatch_health,
    reap_stuck_processing_lists,
    reconcile_backlog_to_zset,
    reconcile_channel_tokens,
)


async def start_workers(count: Optional[int] = None):
    from app.ai.voice.agents.breeze_buddy.dispatch.worker import (
        start_workers as _start_workers,
    )

    return await _start_workers(count)


async def stop_workers() -> None:
    from app.ai.voice.agents.breeze_buddy.dispatch.worker import (
        stop_workers as _stop_workers,
    )

    await _stop_workers()


__all__ = [
    "DISPATCHABLE_EXECUTION_MODES",
    "LeaderElection",
    "acquire_channel_token",
    "cancel_scheduled_lead",
    "clean_stale_bb_locks",
    "init_channel_semaphore",
    "is_dispatchable",
    "monitor_dispatch_health",
    "reap_stuck_processing_lists",
    "reconcile_backlog_to_zset",
    "reconcile_channel_tokens",
    "release_channel_token",
    "schedule_lead",
    "start_promoter",
    "start_workers",
    "stop_promoter",
    "stop_workers",
]
