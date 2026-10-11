"""Business logic handlers for feature flags operations."""

from fastapi import HTTPException

from app.core.logger import logger
from app.schemas import UserInfo
from app.schemas.feature_flags import (
    FeatureFlagDeleteResponse,
    FeatureFlagResponse,
    FeatureFlagUpdate,
    FeatureFlagUpdateResponse,
)
from app.services.live_config.store import get_all_flags, set_all_flags


async def get_feature_flags_handler() -> FeatureFlagResponse:
    """Get all feature flags from Redis (fresh: operators see Redis as it is
    now, not this pod's memo)."""
    try:
        flags = await get_all_flags(fresh=True)
        return FeatureFlagResponse(flags=flags, total_count=len(flags))
    except Exception as e:
        logger.error(f"Failed to fetch feature flags: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to fetch feature flags: {str(e)}"
        )


async def update_feature_flags_handler(
    update: FeatureFlagUpdate, current_user: UserInfo
) -> FeatureFlagUpdateResponse:
    """Update feature flags in Redis."""
    try:
        # Fresh read: a merge built on a memoised copy could drop another
        # pod's write from the last few seconds.
        existing_flags = await get_all_flags(fresh=True)
        updated_flags = {**existing_flags, **update.flags}

        await set_all_flags(updated_flags)

        logger.info(
            f"Feature flags updated by {current_user.username}: "
            f"{list(update.flags.keys())}"
        )

        return FeatureFlagUpdateResponse(
            status="success",
            message=f"Updated {len(update.flags)} feature flag(s)",
            updated_flags=list(update.flags.keys()),
            total_flags=len(updated_flags),
        )
    except Exception as e:
        logger.error(f"Failed to update feature flags: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to update feature flags: {str(e)}"
        )


async def delete_feature_flag_handler(
    flag_key: str, current_user: UserInfo
) -> FeatureFlagDeleteResponse:
    """Delete a feature flag from Redis."""
    try:
        existing_flags = await get_all_flags(fresh=True)

        if flag_key not in existing_flags:
            raise HTTPException(
                status_code=404, detail=f"Feature flag '{flag_key}' not found"
            )

        del existing_flags[flag_key]

        await set_all_flags(existing_flags)

        logger.info(f"Feature flag deleted by {current_user.username}: {flag_key}")

        return FeatureFlagDeleteResponse(
            status="success",
            message=f"Deleted feature flag '{flag_key}'",
            remaining_flags=len(existing_flags),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete feature flag: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to delete feature flag: {str(e)}"
        )
