"""Schemas for the DragonTTS management endpoints."""

from enum import Enum

from pydantic import BaseModel


class ManageDragonTTSAction(str, Enum):
    """Operator action for ``POST /admin/dragontts/manage``.

    The kill-switch (bypass caching) is one action; more may be added as the
    manage endpoint gains operations.
    """

    KILL_SWITCH = "kill_switch"  # engage kill switch -> bypass caching (flag "0")
    RESTORE = "restore"  # restore from kill switch -> resume caching (flag "1")


class ManageDragonTTSRequest(BaseModel):
    """Request body for ``POST /admin/dragontts/manage``."""

    action: ManageDragonTTSAction


class DragonTTSStatus(BaseModel):
    """Current DragonTTS health-gate state (``GET /admin/dragontts/status``)."""

    health: str  # "healthy" | "unhealthy"


class ManageDragonTTSResponse(BaseModel):
    """Response for ``POST /admin/dragontts/manage``: the action performed and
    the resulting DragonTTS state. Intended to grow as the manage endpoint gains
    operations beyond kill/restore.
    """

    action: ManageDragonTTSAction
    health: str  # "healthy" | "unhealthy"
