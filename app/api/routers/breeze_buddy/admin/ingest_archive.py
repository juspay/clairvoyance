"""Admin replay of archived ingest events (app/services/ingest_archive_replay.py)."""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.concurrency import spawn_background_task
from app.core.config.static import INGEST_ARCHIVE_GCS_BUCKET
from app.core.security.authorization import require_admin
from app.schemas import UserInfo
from app.schemas.breeze_buddy.admin.ingest_archive_replay import (
    IngestReplayRequest,
    IngestReplayResponse,
)
from app.services import ingest_archive_replay as replay

router = APIRouter()


@router.post(
    "/admin/ingest-archive/replay",
    response_model=IngestReplayResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def replay_ingest_events(
    req: IngestReplayRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> IngestReplayResponse:
    """Replay archived ``POST /ingest/events`` requests that arrived in
    ``[start, end)``, one by one, through this pod's own events door with
    each merchant's stored auth header. Events dedupe, so a replay is safe to
    repeat. Leads are not replayed.

    A real run answers at once, before the bucket is listed; the listing and
    the replay go on in the background. Watch the logs for the run id
    (component ``ingest.archive.replay``); the report, with the event count,
    lands in ``replay-reports/<run_id>.json`` in the archive bucket. A dry
    run lists first and answers with the count and a sample.

    **RBAC rules:** admin only. 409 while ``BB_INGEST_DB_WRITES_DISABLED`` is
    on, or while another replay runs on this pod.
    """
    require_admin(current_user)
    # Claim the one-run-per-pod guard BEFORE the first await, so a client
    # that retries while this request is still checking gets 409, not a
    # second run.
    run_id = replay.new_run_id()
    if not replay.claim(run_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a replay is already running on this pod",
        )
    names: List[str] = []
    try:
        bucket = await replay.check_replay(req.folder, req.start, req.end)
        if req.dry_run:
            # A dry run's answer IS the listing, so it lists before answering.
            names = await replay.list_replay(bucket, req.folder, req.start, req.end)
    except replay.ReplayError as e:
        replay.release(run_id)
        raise HTTPException(status_code=e.status_code, detail=e.detail) from e
    except Exception:
        replay.release(run_id)
        raise

    if req.dry_run:
        replay.release(run_id)
        return IngestReplayResponse(
            run_id=None,
            folder=req.folder,
            start=req.start.isoformat(),
            end=req.end.isoformat(),
            objects=len(names),
            started=False,
            report=None,
            sample=names[:5],
        )

    # A real run answers now and lists in the background: listing a big
    # window can outlast the ingress's 5 s, and the caller needs its run id.
    spawn_background_task(
        replay.run_replay(run_id, req.folder, req.start, req.end),
        name=f"ingest-archive-replay-{run_id}",
    )
    return IngestReplayResponse(
        run_id=run_id,
        folder=req.folder,
        start=req.start.isoformat(),
        end=req.end.isoformat(),
        objects=None,
        started=True,
        report=f"gs://{INGEST_ARCHIVE_GCS_BUCKET}/{replay.REPORT_FOLDER}/{run_id}.json",
        sample=[],
    )


__all__ = ["router"]
