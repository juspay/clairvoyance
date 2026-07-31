"""Health, readiness, and graceful-drain endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.drain import engage_drain, is_draining

router = APIRouter()


@router.get("/health")
async def health(request: Request):
    return {"status": "ok", "providers": request.app.state.registry.configured()}


@router.get("/providers")
async def providers(request: Request):
    return {"providers": request.app.state.registry.configured()}


@router.get("/ready")
async def ready() -> dict:
    """Readiness probe. 503 while draining so k8s stops routing NEW traffic to
    this pod (complements endpoint removal on termination). Stays 200 on the
    liveness probe (/health) so k8s doesn't restart the pod mid-drain."""
    if is_draining():
        raise HTTPException(status_code=503, detail="draining")
    return {"status": "ready"}


@router.get("/drain")
async def drain() -> dict:
    """k8s preStop hook. Marks the pod draining, fires the kill-switch Slack
    alert, and tells clairvoyance to bypass us BEFORE SIGTERM arrives — then
    the lifespan shutdown waits for in-flight requests to finish. See app.drain."""
    await engage_drain()
    return {"status": "draining"}
