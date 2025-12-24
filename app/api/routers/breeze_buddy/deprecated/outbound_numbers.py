from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from starlette.responses import JSONResponse

from app.core.logger import logger
from app.core.security.jwt import get_breeze_buddy_session, get_current_user
from app.database.accessor import (
    create_outbound_number,
    disable_outbound_number,
)
from app.database.accessor import get_all_outbound_numbers
from app.database.accessor import (
    get_all_outbound_numbers as get_all_outbound_numbers_db,
)
from app.database.accessor import (
    get_outbound_number_by_id,
)
from app.schemas import CreateOutboundNumberRequest, TokenData

router = APIRouter()


@router.post("/outbound-number")
async def add_outbound_number(
    number: CreateOutboundNumberRequest,
    current_user: TokenData = Depends(get_current_user),
):
    """
    Adds a new outbound number to the database.
    Requires JWT authentication.
    """
    logger.info(
        f"Authenticated user {current_user.user_id} adding new outbound number: {number.number}"
    )

    try:
        outbound_number = await create_outbound_number(
            id=str(uuid4()),
            number=number.number,
            provider=number.provider,
            status=number.status,
            merchant_id=number.merchant_id,
            shop_identifier=number.shop_identifier,
            channels=0,
            maximum_channels=number.maximum_channels,
        )

        if outbound_number:
            logger.info(
                f"Outbound number {number.number} added successfully with ID {outbound_number.id}"
            )
            return outbound_number
        else:
            logger.error(f"Failed to add outbound number {number.number}")
            return JSONResponse(
                status_code=400, content={"detail": "Failed to add outbound number"}
            )

    except Exception as e:
        logger.error(f"Error adding outbound number: {e}")
        return JSONResponse(
            status_code=400,
            content={"detail": f"Error adding outbound number: {str(e)}"},
        )


@router.get("/outbound-number")
async def get_outbound_number(
    id: str = None, current_user: TokenData = Depends(get_current_user)
):
    """
    Gets an outbound number from the database based on the provided query parameters.
    Requires JWT authentication.
    """
    logger.info(f"Authenticated user {current_user.user_id} requesting outbound number")

    try:
        if id:
            outbound_number = await get_outbound_number_by_id(id)
            if outbound_number:
                return outbound_number
            else:
                return []
        else:
            return await get_all_outbound_numbers()

    except Exception as e:
        logger.error(f"Error getting outbound number: {e}")
        return JSONResponse(
            status_code=400,
            content={"detail": f"Error getting outbound number: {str(e)}"},
        )


@router.delete("/outbound-number/{number_id}")
async def delete_outbound_number(
    number_id: str, current_user: TokenData = Depends(get_current_user)
):
    """
    Disables an outbound number in the database.
    Requires JWT authentication.
    """
    logger.info(
        f"Authenticated user {current_user.user_id} disabling outbound number: {number_id}"
    )

    try:
        outbound_number = await disable_outbound_number(number_id)

        if outbound_number:
            logger.info(f"Outbound number {number_id} disabled successfully")
            return outbound_number
        else:
            logger.error(f"Failed to disable outbound number {number_id}")
            return JSONResponse(
                status_code=400, content={"detail": "Failed to disable outbound number"}
            )

    except Exception as e:
        logger.error(f"Error disabling outbound number: {e}")
        return JSONResponse(
            status_code=400,
            content={"detail": f"Error disabling outbound number: {str(e)}"},
        )


@router.get("/breeze/order-confirmation/outbound-numbers", include_in_schema=False)
async def get_outbound_numbers_for_dashboard(
    session: dict = Depends(get_breeze_buddy_session),
):
    """
    Provides all outbound numbers for the dashboard.
    """
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return await get_all_outbound_numbers_db()
