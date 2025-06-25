from typing import List

from app.core.logger import logger
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from .dummy import tools as dummy_tools, tool_functions as dummy_tool_functions
from .system import tools as system_tools, tool_functions as system_tool_functions
from . import juspay
from . import breeze


def initialize_tools(
    mode: str,
    breeze_token: str | None = None,
    euler_token: str | None = None,
    shop_url: str | None = None,
    shop_id: str | None = None,
    shop_type: str | None = None,
):
    """
    Initializes tools based on the operating mode and available tokens.

    :param mode: The mode of operation, "test" or "live".
    :param breeze_token: The Breeze token, if available.
    :param euler_token: The Euler token, if available.
    :param shop_url: The shop URL, if available.
    :param shop_id: The shop ID, if available.
    :param shop_type: The shop type, if available.
    """
    providers = []
    if breeze_token:
        providers.append("breeze")
    if euler_token:
        providers.append("euler")

    logger.info(f"Initializing tools in '{mode}' mode with providers: {providers}")
    logger.info(f"Shop context: id={shop_id}, url={shop_url}, type={shop_type}")
    all_tools = []
    all_tool_functions = {}

    # System tools are always available
    all_tools.extend(system_tools.standard_tools)
    all_tool_functions.update(system_tool_functions)
    logger.info(f"Loaded {len(system_tools.standard_tools)} system tools.")

    # Dummy tools are only available in test mode
    if mode == "test":
        all_tools.extend(dummy_tools.standard_tools)
        all_tool_functions.update(dummy_tool_functions)
        logger.info(f"Loaded {len(dummy_tools.standard_tools)} dummy tools for test mode.")
    else:
        logger.info("Skipping dummy tools in live mode.")
        if "euler" in providers:
            juspay.analytics.euler_token = euler_token
            all_tools.extend(juspay.tools.standard_tools)
            all_tool_functions.update(juspay.tool_functions)
            logger.info(f"Loaded {len(juspay.tools.standard_tools)} real-time Juspay tools.")
        if "breeze" in providers and shop_id and shop_url and shop_type:
            breeze.analytics.breeze_token = breeze_token
            breeze.analytics.shop_id = shop_id
            breeze.analytics.shop_url = shop_url
            breeze.analytics.shop_type = shop_type
            all_tools.extend(breeze.tools.standard_tools)
            all_tool_functions.update(breeze.tool_functions)
            logger.info(f"Loaded {len(breeze.tools.standard_tools)} real-time Breeze tools.")

    # Create a single ToolsSchema with all aggregated tools
    final_tools = ToolsSchema(standard_tools=all_tools)
    logger.info(f"Total tools initialized: {len(all_tools)}")

    return final_tools, all_tool_functions


__all__ = ["initialize_tools"]