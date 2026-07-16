"""SmallWebRTC transport endpoints for Breeze Buddy (device/embedded clients).

POST  /smallwebrtc/offer  - SDP offer -> answer, validates lead, spawns bot in-process
PATCH /smallwebrtc/offer  - ICE trickle / renegotiation
"""

from fastapi import APIRouter, Depends, Request

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo
from app.schemas.breeze_buddy.smallwebrtc import (
    SmallWebRTCAnswerResponse,
    SmallWebRTCOfferRequest,
    SmallWebRTCPatchRequestBody,
    SmallWebRTCPatchResponse,
)

from .handlers import smallwebrtc_offer_handler, smallwebrtc_patch_handler

router = APIRouter()


@router.post("/smallwebrtc/offer", response_model=SmallWebRTCAnswerResponse)
async def smallwebrtc_offer(
    offer: SmallWebRTCOfferRequest,
    request: Request,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> SmallWebRTCAnswerResponse:
    # request_host feeds ESP32 SDP munging when the client self-identifies as
    # esp32 and no BB_WEBRTC_ESP32_HOST override is set.
    return await smallwebrtc_offer_handler(
        offer, request_host=request.headers.get("host")
    )


@router.patch("/smallwebrtc/offer", response_model=SmallWebRTCPatchResponse)
async def smallwebrtc_patch(
    body: SmallWebRTCPatchRequestBody,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> SmallWebRTCPatchResponse:
    return await smallwebrtc_patch_handler(body)
