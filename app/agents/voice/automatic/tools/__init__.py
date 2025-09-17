from pipecat.adapters.schemas.tools_schema import ToolsSchema

from app.agents.voice.automatic.tools.utils import filter_tools_by_authorization
from app.agents.voice.automatic.types import Mode
from app.core.config import (
    AUTOMATIC_WRITE_ACTIONS_AUTHORIZED_USERS,
    ENABLE_CHARTS,
    ENABLE_SEARCH_GROUNDING,
)
from app.core.logger import logger

from . import breeze, charts, internet, juspay
from .dummy import tool_functions as dummy_tool_functions
from .dummy import tools as dummy_tools
from .system import tool_functions as system_tool_functions
from .system import tools as system_tools


def initialize_tools(
    mode: str,
    breeze_token: str | None = None,
    euler_token: str | None = None,
    shop_url: str | None = None,
    shop_id: str | None = None,
    shop_type: str | None = None,
    merchant_id: str | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    user_email: str | None = None,
    reseller_id: str | None = None,
):
    """
    Initializes tools based on the operating mode and available tokens.

    :param mode: The mode of operation, "test" or "live".
    :param breeze_token: The Breeze token, if available.
    :param euler_token: The Euler token, if available.
    :param shop_url: The shop URL, if available.
    :param shop_id: The shop ID, if available.
    :param shop_type: The shop type, if available.
    :param session_id: The client session ID for tool calls (now receives client_sid).
    :param merchant_id: The merchant ID, if available.
    :param user_id: The user ID, if available.
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

    # Internet tools are always available
    if ENABLE_SEARCH_GROUNDING:
        all_tools.extend(internet.tools.standard_tools)
        all_tool_functions.update(internet.tool_functions)
        logger.info(f"Loaded {len(internet.tools.standard_tools)} internet tools.")
    else:
        logger.info("Internet search tools are disabled.")

    if ENABLE_CHARTS:
        all_tools.extend(charts.generate_ui.standard_tools)
        all_tool_functions.update(charts.tool_functions)
        logger.info(f"Loaded {len(charts.tool_functions)} chart tools.")

    # Dummy tools are only available in test mode
    if mode == Mode.TEST.value:
        all_tools.extend(dummy_tools.standard_tools)
        all_tool_functions.update(dummy_tool_functions)
        logger.info(
            f"Loaded {len(dummy_tools.standard_tools)} dummy tools for test mode."
        )
    else:
        logger.info("Skipping dummy tools in live mode.")
        if "euler" in providers:
            juspay.analytics.euler_token = euler_token
            juspay.analytics.merchant_id = merchant_id
            all_tools.extend(juspay.tools.standard_tools)
            all_tool_functions.update(juspay.tool_functions)
            logger.info(
                f"Loaded {len(juspay.tools.standard_tools)} real-time Juspay tools."
            )
            logger.info(f"Set merchant_id for Juspay tools: {merchant_id}")
        if "breeze" in providers and shop_id and shop_url and shop_type:
            breeze.analytics.breeze_token = breeze_token
            breeze.analytics.shop_id = shop_id
            breeze.analytics.shop_url = shop_url
            breeze.analytics.shop_type = shop_type
            breeze.analytics.sessionId = session_id
            breeze.analytics.reseller_id = reseller_id
            all_tools.extend(breeze.tools.standard_tools)
            all_tool_functions.update(breeze.tool_functions)
            logger.info(
                f"Loaded {len(breeze.tools.standard_tools)} real-time Breeze analytics tools."
            )
        if (
            "breeze" in providers
            and breeze_token
            and shop_id
            and shop_url
            and merchant_id
        ):
            breeze.configuration.shop_id = shop_id
            breeze.configuration.shop_url = shop_url
            breeze.configuration.merchant_id = merchant_id
            breeze.configuration.user_id = user_id or "unknown"
            breeze.configuration.breeze_token = breeze_token
            all_tools.extend(breeze.configuration_tools.standard_tools)
            all_tool_functions.update(breeze.configuration_tool_functions)
            logger.info(
                f"Loaded {len(breeze.configuration_tools.standard_tools)} real-time Breeze configuration tools."
            )

    tools = ToolsSchema(standard_tools=all_tools)

    if AUTOMATIC_WRITE_ACTIONS_AUTHORIZED_USERS:
        logger.info(f"Total tools before filtering: {len(all_tools)}")

        filtered_tools, filtered_tool_functions = filter_tools_by_authorization(
            all_tools, all_tool_functions, user_email
        )

        logger.info(f"Total tools after filtering: {len(filtered_tools)}")
        return ToolsSchema(standard_tools=filtered_tools), filtered_tool_functions

    logger.info(f"Total tools initialized: {len(all_tools)}")
    return tools, all_tool_functions


__all__ = ["initialize_tools"]
