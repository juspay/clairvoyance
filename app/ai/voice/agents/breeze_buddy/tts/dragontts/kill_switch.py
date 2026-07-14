"""DragonTTS kill switch — operator-facing control surface.

Manual, two-way control of the one-way health flag from :mod:`health`:
``set_dragontts_health`` (the admin kill/restore action exposed at
``POST /admin/dragontts/manage``) and ``get_dragontts_status`` (the
status read exposed at ``GET /admin/dragontts/status``). Both layer on top of
the health primitives; this module adds no state of its own.

The automatic monitor (:mod:`monitor`) only ever marks the flag unhealthy
(1->0); ``set_dragontts_health`` is the only way to restore it. Note: if
DragonTTS is still actually down, the monitor's next failed probe re-marks it
``"0"``, so a restore only sticks once DragonTTS has recovered.
"""

from __future__ import annotations

from app.ai.voice.agents.breeze_buddy.tts.dragontts.health import (
    _set_health,
    is_dragontts_healthy,
)

__all__ = ["set_dragontts_health", "get_dragontts_status"]


async def set_dragontts_health(healthy: bool) -> None:
    """Manually flip the DragonTTS health flag (admin endpoint, both ways).

    A single Redis SET via ``_set_health``, so it's atomic — no lock needed.
    """
    await _set_health(healthy)


async def get_dragontts_status() -> dict:
    """Return the DragonTTS health state for the status endpoint."""
    healthy = await is_dragontts_healthy()
    return {"health": "healthy" if healthy else "unhealthy"}
