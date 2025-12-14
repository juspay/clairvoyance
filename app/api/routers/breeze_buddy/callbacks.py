from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response

from app.ai.voice.agents.breeze_buddy.managers.calls import (
    handle_unanswered_calls,
    update_call_recording,
)
from app.core.logger import logger

router = APIRouter()


@router.get("/{provider}/callback/details")
async def callback_details(
    request: Request, provider: str, background_tasks: BackgroundTasks
):
    query_params = dict(request.query_params)
    logger.info(f"Received call-details with {provider} query params: {query_params}")

    if provider.lower() != "exotel":
        raise HTTPException(
            status_code=404, detail="Feature not supported for this service provider"
        )

    call_sid = query_params.get("CallSid")
    provider_recording_url = query_params.get("Stream[RecordingUrl]")

    if provider_recording_url and call_sid:
        logger.info(
            f"Extracted recording_url: {provider_recording_url} and call_sid: {call_sid}"
        )
        background_tasks.add_task(
            update_call_recording, call_sid, provider_recording_url, provider.lower()
        )

    return Response(status_code=200)


@router.post("/{provider}/callback/details")
async def callback_details_post(
    request: Request, provider: str, background_tasks: BackgroundTasks
):
    """
    Logs the request body and returns a 200 OK response.
    """
    form = await request.form()
    logger.info(f"Received callback from {provider} with form data: {form}")

    if provider.lower() != "twilio":
        raise HTTPException(
            status_code=404, detail="Feature not supported for this service provider"
        )

    call_sid = form.get("CallSid")
    provider_recording_url = form.get("RecordingUrl")

    if provider_recording_url and call_sid:
        logger.info(
            f"Extracted recording_url: {provider_recording_url} and call_sid: {call_sid}"
        )
        background_tasks.add_task(
            update_call_recording, call_sid, provider_recording_url, provider.lower()
        )

    return Response(status_code=200)


@router.post("/{provider}/callback/status")
async def callback_status(request: Request, provider: str):
    """
    Logs the request body and returns a 200 OK response.
    """
    form = await request.form()
    logger.info(f"Received callback from {provider} with form data: {form}")

    call_sid = form.get("CallSid")
    call_status = None

    if provider.lower() == "twilio":
        call_status = form.get("CallStatus")
    elif provider.lower() == "exotel":
        call_status = form.get("Status")

    logger.info(
        f"Extracted call_sid: {call_sid} and call_status: {call_status} from {provider}"
    )

    if call_status in ("no-answer", "failed", "busy"):
        logger.info(f"Call with SID {call_sid} failed with status: {call_status}")
        await handle_unanswered_calls(call_sid)

    return Response(status_code=200)
