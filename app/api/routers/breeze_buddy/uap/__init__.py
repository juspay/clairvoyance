from typing import Any, Dict

from fastapi import APIRouter, Request

from app.api.routers.breeze_buddy.uap import handlers

router = APIRouter()


@router.post("/webhook")
async def uap_webhook(request: Request) -> Dict[str, Any]:
    return await handlers.handle_webhook(request)
