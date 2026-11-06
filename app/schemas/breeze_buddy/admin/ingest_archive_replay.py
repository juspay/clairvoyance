"""Request and response models for ``POST /admin/ingest-archive/replay``."""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import AwareDatetime, BaseModel, Field


class IngestReplayRequest(BaseModel):
    """Which archived ``POST /ingest/events`` requests to replay."""

    start: AwareDatetime = Field(
        description="Arrival time from, inclusive, with an offset "
        "(e.g. 2026-10-27T01:00:00+05:30)."
    )
    end: AwareDatetime = Field(
        description="Arrival time to, exclusive. At most 24 hours after start."
    )
    folder: Literal["parked", "copied"] = Field(
        default="parked",
        description="parked: requests answered from GCS alone (never stored). "
        "copied: requests also sent to Postgres; replaying them fills only "
        "what the store refused, the rest answers duplicate.",
    )
    dry_run: bool = Field(
        default=False, description="List what would be replayed, replay nothing."
    )


class IngestReplayResponse(BaseModel):
    run_id: Optional[str] = Field(
        description="None on a dry run. Search the logs for it, component "
        "ingest.archive.replay."
    )
    folder: str
    start: str
    end: str
    objects: Optional[int] = Field(
        description="Event objects in the window, on a dry run. None on a real "
        "run: it lists in the background; the count is in its log lines and "
        "report."
    )
    started: bool = Field(description="False on a dry run.")
    report: Optional[str] = Field(
        description="Where the report lands when the run finishes."
    )
    sample: List[str] = Field(
        default_factory=list,
        description="The first few object names, on a dry run.",
    )
