"""Health + introspection endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/health")
async def health(request: Request):
    return {"status": "ok", "providers": request.app.state.registry.configured()}


@router.get("/providers")
async def providers(request: Request):
    return {"providers": request.app.state.registry.configured()}
