"""
OpenFeature Service

This module provides the main OpenFeature initialization and a unified
interface for feature flag management across different providers.
"""

import os
from typing import Optional
from openfeature import api
from loguru import logger

# Import DevCycle provider
from .providers.dev_cycle import create_devcycle_provider, is_devcycle_available
from .providers.dev_cycle.feature_flags import FeatureFlagService


def initialize_open_feature() -> bool:
    """
    Initialize OpenFeature with the appropriate provider.
    
    Currently supports DevCycle provider, but designed to be extensible
    for other providers in the future.
    
    Returns:
        bool: True if initialization was successful, False otherwise
    """
    try:
        # Try to initialize DevCycle provider first
        if is_devcycle_available():
            devcycle_provider = create_devcycle_provider()
            if devcycle_provider:
                api.set_provider(devcycle_provider)
                logger.info("OpenFeature initialized with DevCycle provider")
                return True
            else:
                logger.warning("Failed to create DevCycle provider")
        else:
            logger.info("DEVCYCLE_SERVER_SDK_KEY not configured, OpenFeature will use Envirnonment fallback")
        
        # Fallback mode - no provider set, will use environment fallback
        logger.info("OpenFeature initialized with environment fallback mode")
        return True
        
    except Exception as e:
        logger.error(f"Failed to initialize OpenFeature: {e}")
        return False


def get_feature_flag_service() -> FeatureFlagService:
    """
    Get the feature flag service instance.
    
    Returns:
        FeatureFlagService: Configured feature flag service
    """
    return FeatureFlagService()


# Initialize OpenFeature when the module is imported
_initialization_success = initialize_open_feature()

# Create global feature flag service instance
feature_flags = get_feature_flag_service()

# Export the main interface
__all__ = [
    'feature_flags',
    'FeatureFlagService',
    'initialize_open_feature',
    'get_feature_flag_service'
]
