"""
Input Collection Configuration Utilities

This module handles input collection mode for nodes that need multi-segment
input (phone numbers, addresses, etc.). It provides a helper to extract the
user_speech_timeout from a node's input_collection config.

The actual strategy switching is done by the interruption module — this module
just determines the correct user_speech_timeout value for each node transition.

The default user_speech_timeout is 0.0 (immediate turn end on finalized transcript),
which is the normal behavior for conversational nodes. Collection nodes override
this to a higher value (e.g., 3.0s) so that multiple speech segments separated
by natural pauses are accumulated into a single user turn.
"""

from typing import Optional

from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.types import (
    BackChannelConfig,
    InputCollectionConfig,
)
from app.core.logger import logger

# Default user_speech_timeout used for all non-collection nodes.
# 0.0 means the turn ends immediately when a finalized transcript arrives.
DEFAULT_USER_SPEECH_TIMEOUT = 0.0


def get_node_user_speech_timeout(context: TemplateContext, node_name: str) -> float:
    """Get the user_speech_timeout for a node based on its input_collection config.

    If the node has an enabled input_collection config, returns its
    user_speech_timeout value. Otherwise returns DEFAULT_USER_SPEECH_TIMEOUT (0.0).

    Args:
        context: Template context with bot state access
        node_name: Name of the target node

    Returns:
        user_speech_timeout in seconds
    """
    bot = context.bot
    if not hasattr(bot, "flow_config") or not bot.flow_config:
        return DEFAULT_USER_SPEECH_TIMEOUT

    nodes = bot.flow_config.get("nodes", {})
    if node_name not in nodes:
        return DEFAULT_USER_SPEECH_TIMEOUT

    node_config = nodes[node_name]
    collection_config = node_config.get("input_collection")
    if collection_config is None:
        return DEFAULT_USER_SPEECH_TIMEOUT

    # collection_config may be an InputCollectionConfig object or a dict
    if isinstance(collection_config, dict):
        collection_config = InputCollectionConfig.model_validate(collection_config)

    if not collection_config.enabled:
        return DEFAULT_USER_SPEECH_TIMEOUT

    logger.info(
        f"Input collection enabled for node '{node_name}': "
        f"user_speech_timeout={collection_config.user_speech_timeout}s "
        f"(call: {context.call_sid or 'unknown'})"
    )
    return collection_config.user_speech_timeout


def get_node_back_channel_config(
    context: TemplateContext, node_name: str
) -> Optional[BackChannelConfig]:
    """Get the BackChannelConfig for a node, or None if not configured/enabled.

    Returns the BackChannelConfig only when the node has input_collection
    enabled AND back_channel enabled. Returns None otherwise.

    Args:
        context: Template context with bot state access
        node_name: Name of the target node

    Returns:
        BackChannelConfig if enabled, None otherwise
    """
    bot = context.bot
    if not hasattr(bot, "flow_config") or not bot.flow_config:
        return None

    nodes = bot.flow_config.get("nodes", {})
    if node_name not in nodes:
        return None

    node_config = nodes[node_name]
    collection_config = node_config.get("input_collection")
    if collection_config is None:
        return None

    # collection_config may be an InputCollectionConfig object or a dict
    if isinstance(collection_config, dict):
        collection_config = InputCollectionConfig.model_validate(collection_config)

    if not collection_config.enabled:
        return None

    back_channel = collection_config.back_channel
    if back_channel is None or not back_channel.enabled:
        return None

    logger.info(
        f"Back-channel enabled for node '{node_name}': "
        f"delay={back_channel.delay_secs}s, "
        f"min_interval={back_channel.min_interval_secs}s, "
        f"messages={back_channel.messages} "
        f"(call: {context.call_sid or 'unknown'})"
    )
    return back_channel
