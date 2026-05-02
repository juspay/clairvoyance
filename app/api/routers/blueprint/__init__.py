from fastapi import APIRouter

from app.api.routers.blueprint.chat import router as chat_router
from app.api.routers.blueprint.sessions import router as sessions_router

router = APIRouter()
router.include_router(sessions_router, prefix="", tags=["blueprint-sessions"])
router.include_router(chat_router, prefix="", tags=["blueprint-chat"])
