from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import RedirectResponse
from starlette.responses import FileResponse

from app.ai.voice.agents.breeze_buddy.types.models import LoginRequest
from app.core.config.static import (
    BREEZE_BUDDY_DASHBOARD_PASSWORD,
    BREEZE_BUDDY_DASHBOARD_USERNAME,
    BREEZE_BUDDY_SESSION_SECRET_KEY,
    JWT_ALGORITHM,
)

router = APIRouter()


@router.get("/login", include_in_schema=False)
async def get_login_page():
    return FileResponse("app/ai/voice/agents/breeze_buddy/dashboard/login.html")


@router.post("/login", include_in_schema=False)
async def login(login_request: LoginRequest, response: Response):
    if (
        login_request.username == BREEZE_BUDDY_DASHBOARD_USERNAME
        and login_request.password == BREEZE_BUDDY_DASHBOARD_PASSWORD
    ):
        session_data = {
            "username": login_request.username,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        }
        session_cookie = jwt.encode(
            session_data, BREEZE_BUDDY_SESSION_SECRET_KEY, algorithm=JWT_ALGORITHM
        )
        response.set_cookie(key="session", value=session_cookie, httponly=True)
        return {"message": "Login successful"}
    raise HTTPException(status_code=401, detail="Invalid credentials")


@router.get("/logout", include_in_schema=False)
async def logout():
    response = RedirectResponse(url="/agent/voice/breeze-buddy/login")
    response.delete_cookie("session")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response
