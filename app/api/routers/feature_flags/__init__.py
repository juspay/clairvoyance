"""
Feature Flags Management Router

Provides CRUD operations for Redis-based feature flags.
Frontend calls these endpoints directly to read/write flags.
"""

from fastapi import APIRouter, Depends

from app.schemas import UserInfo
from app.schemas.feature_flags import (
    FeatureFlagDeleteResponse,
    FeatureFlagResponse,
    FeatureFlagUpdate,
    FeatureFlagUpdateResponse,
)

from .handlers import (
    delete_feature_flag_handler,
    get_feature_flags_handler,
    update_feature_flags_handler,
)
from .rbac import get_current_user_with_role, require_admin

router = APIRouter()


@router.get("/feature-flags", response_model=FeatureFlagResponse)
async def get_feature_flags(
    current_user: UserInfo = Depends(get_current_user_with_role),
):
    return await get_feature_flags_handler()


@router.post("/feature-flags", response_model=FeatureFlagUpdateResponse)
async def update_feature_flags(
    update: FeatureFlagUpdate,
    current_user: UserInfo = Depends(get_current_user_with_role),
):
    require_admin(current_user)
    return await update_feature_flags_handler(update, current_user)


@router.delete("/feature-flags/{flag_key}", response_model=FeatureFlagDeleteResponse)
async def delete_feature_flag(
    flag_key: str,
    current_user: UserInfo = Depends(get_current_user_with_role),
):
    require_admin(current_user)
    return await delete_feature_flag_handler(flag_key, current_user)
