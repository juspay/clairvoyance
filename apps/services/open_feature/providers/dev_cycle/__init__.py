"""
DevCycle OpenFeature Provider

This module handles the initialization and setup of the DevCycle provider
for OpenFeature, providing a clean interface for feature flag management.
"""

import os
from typing import Optional, Dict, Any
from devcycle_python_sdk import DevCycleLocalClient, DevCycleLocalOptions
from openfeature.provider import FeatureProvider
from loguru import logger



def create_devcycle_provider() -> Optional[FeatureProvider]:
    """
    Create and configure a DevCycle OpenFeature provider.
    
    Returns:
        FeatureProvider: Configured DevCycle provider, or None if SDK key is not found
    """
    sdk_key = os.environ.get("DEVCYCLE_SERVER_SDK_KEY")
    
    if not sdk_key:
        logger.warning("DEVCYCLE_SERVER_SDK_KEY not found, DevCycle provider will not be initialized")
        return None
    
    try:
        # Create options for optimal performance
        options = DevCycleLocalOptions(
            config_polling_interval_ms=10000,  # Poll every 10 seconds
            disable_realtime_updates=False,    # Enable realtime updates
        )
        
        # Create DevCycle client instance
        devcycle_client = DevCycleLocalClient(sdk_key, options)
        
        # Get the OpenFeature provider
        provider = devcycle_client.get_openfeature_provider()
        
        logger.info("DevCycle OpenFeature provider created successfully")
        return provider
        
    except Exception as e:
        logger.error(f"Failed to create DevCycle provider: {e}")
        return None


def is_devcycle_available() -> bool:
    """
    Check if DevCycle provider is available (SDK key is configured).
    
    Returns:
        bool: True if DevCycle can be initialized, False otherwise
    """
    return bool(os.environ.get("DEVCYCLE_SERVER_SDK_KEY"))
