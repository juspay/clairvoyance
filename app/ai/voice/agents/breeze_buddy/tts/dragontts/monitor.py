"""Scheduler-facing DragonTTS health monitor — probe + alert.

This module is the composition point that wires the low-level health gate
(:mod:`health`) to the dispatch alerting layer. It is imported only by the app
lifespan (``app.main``) to register the scheduler task, never by the call-time
TTS path (``tts/__init__``).

Keeping the monitor out of :mod:`health` is what breaks the import cycle:
``tts/__init__`` imports ``health``, and ``health`` must not reach up into
``dispatch`` (which pulls in ``worker`` -> ``managers.utils`` -> back to
``tts/__init__``). Because only this module — not ``health`` — imports
``dispatch.alerts``, and nothing on the call path imports this module, all
imports can live at module top with no lazy import.

It cannot live inside ``dispatch/`` (e.g. next to the analogous
``monitor_dispatch_health`` in ``reconcilers.py``): that is precisely the tree
``health`` must not import, so co-locating it there would re-introduce the cycle.
"""

from __future__ import annotations

from app.ai.voice.agents.breeze_buddy.dispatch.alerts import raise_dragontts_degraded
from app.ai.voice.agents.breeze_buddy.tts.dragontts.health import (
    check_dragontts_health,
)

__all__ = ["monitor_dragontts_health"]


async def monitor_dragontts_health() -> None:
    """One scheduler tick: probe DragonTTS (marks the flag down on failure);
    page on-call if unhealthy.

    The one-way mark-down lives in ``check_dragontts_health``; this function
    only layers the alert on top.
    """
    healthy = await check_dragontts_health()
    if not healthy:
        await raise_dragontts_degraded()
