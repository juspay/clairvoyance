import json
from enum import Enum

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams

from app.core.logger import logger

from .utils import (
    format_announcement_html,
    get_current_shop_config_data,
    patch_shop_config,
    remove_html_tags,
)

# These will be set by the initializer
breeze_token: str | None = None
shop_id: str | None = None
shop_url: str | None = None
merchant_id: str | None = None
user_id: str | None = None


class BannerAction(str, Enum):
    FETCH = "fetch"
    ADD = "add"
    UPDATE = "update"
    REMOVE = "remove"


async def manage_announcement_banner(params: FunctionCallParams):
    """
    Manages all announcement banner operations including fetching, creating, updating, and deleting.

    Actions you can perform:
    - fetch: Retrieve the current announcement banner.
    - add: Create a new announcement banner.
    - update: Change the content of an existing announcement banner.
    - remove: Remove an announcement banner.

    The tool updates both login page and payment page announcements simultaneously.
    """
    try:
        action = params.arguments.get("action")
        description = params.arguments.get("description")

        # Validate action
        if not action:
            await params.result_callback(
                {
                    "success": False,
                    "Tool Error": " [manage_announcement_banner] Action is required.",
                }
            )
            return

        logger.info(f"Banner operation started: {action}")

        # For all actions, we need shop_url
        if not shop_url:
            logger.error(
                "Tool Error: [manage_announcement_banner] Banner operation called without required shop URL."
            )
            await params.result_callback(
                {
                    "success": False,
                    "Tool Error": "[manage_announcement_banner] Banner tool is not configured with shop information.",
                }
            )
            return

        # Fetch shop configuration data once
        try:
            config = await get_current_shop_config_data(shop_url)
        except ValueError as e:
            logger.error(
                f"Tool Error: [manage_announcement_banner] Failed to get shop configuration: {str(e)}"
            )
            await params.result_callback(
                {
                    "success": False,
                    "error": f"Tool Error: [manage_announcement_banner] Could not retrieve shop configuration: {str(e)}",
                }
            )
            return

        # For fetch action, we only need shop_url
        if action == BannerAction.FETCH:
            # Extract announcement text from config (both keys have the same content)
            announcement_text = config.get("loginPageAnnouncementText", "")

            # Check if announcement is present
            if not announcement_text:
                # Prepare result indicating no banner is set
                result = {
                    "success": True,
                    "announcement": None,
                    "message": "No announcement banner is currently set for this merchant.",
                }
                logger.info(f"No announcement banner found for {shop_url}")
            else:
                clean_announcement = remove_html_tags(announcement_text)

                if not clean_announcement or len(clean_announcement) == 0:
                    result = {
                        "success": True,
                        "announcement": None,
                        "message": "No announcement content found.",
                    }
                    logger.info(f"No announcement content found for {shop_url}")
                else:
                    result = {"success": True, "announcement": clean_announcement}

                    logger.info(
                        f"Fetched announcement for {shop_url}: {clean_announcement}"
                    )

            # Return the result
            await params.result_callback(result)
            return

        # For all other actions, we need full shop information
        if not shop_id or not merchant_id or not breeze_token:
            logger.error(
                "Tool Error: [manage_announcement_banner] Banner operation called without required context (breezeToken, shopId, merchantId)."
            )
            await params.result_callback(
                {
                    "success": False,
                    "Tool Error": " [manage_announcement_banner] Banner tool is not configured with shop information.",
                }
            )
            return

        # Validate description for add and update actions
        if (
            action == BannerAction.ADD or action == BannerAction.UPDATE
        ) and not description:
            await params.result_callback(
                {
                    "success": False,
                    "Tool Error": " [manage_announcement_banner] Description is required when adding or updating a banner.",
                }
            )
            return

        config_to_patch = {**config}

        # Define both config keys that will be updated
        login_page_key = "loginPageAnnouncementText"
        payment_page_key = "announcementBannerText"

        if action == BannerAction.ADD or action == BannerAction.UPDATE:
            # Format the description with HTML styling
            formatted_description = format_announcement_html(description)

            # Update both announcement types with the same formatted content
            config_to_patch[login_page_key] = formatted_description
            config_to_patch[payment_page_key] = formatted_description

            logger.info(f"Setting both announcement types to '{formatted_description}'")
        elif action == BannerAction.REMOVE:
            # Remove both announcement types
            if login_page_key in config_to_patch:
                del config_to_patch[login_page_key]
            if payment_page_key in config_to_patch:
                del config_to_patch[payment_page_key]

            logger.info("Deleted both announcement types")

        if merchant_id == config.get("merchantId"):
            patch_result = await patch_shop_config(
                shop_url, user_id, config_to_patch, breeze_token
            )

            logger.info(
                f"Updated shop config for {shop_url} with changes to both announcement types"
            )
            logger.info(
                f"Config changes: {json.dumps({'loginPageAnnouncementText': config_to_patch.get('loginPageAnnouncementText'), 'announcementBannerText': config_to_patch.get('announcementBannerText')})}"
            )

            # If patch was successful, fetch and log the updated configuration
            if patch_result and patch_result.get("status") != "failure":
                try:
                    updated_config = await get_current_shop_config_data(shop_url)
                    logger.info(
                        f"Updated shop configuration: {json.dumps(updated_config)}"
                    )
                except ValueError as e:
                    logger.error(
                        f"Tool Error: [manage_announcement_banner] Failed to fetch updated configuration: {e}"
                    )

            # Return the patch_result
            await params.result_callback(patch_result)
        else:
            await params.result_callback(
                {
                    "success": False,
                    "Tool Error": "[manage_announcement_banner] Merchant ID mismatch. Cannot update configuration.",
                }
            )
            return

    except Exception as e:
        error_message = str(e)
        logger.error(f"Tool Error: [manage_announcement_banner] Error: {error_message}")
        await params.result_callback(
            {
                "success": False,
                "Tool Error": f" [manage_announcement_banner] Error: {error_message}",
            }
        )


manage_announcement_banner_function = FunctionSchema(
    name="manage_announcement_banner",
    description="""Manages all announcement banner operations including fetching, creating, updating, and deleting.

This tool can handle:
- **Login Page Announcements**: Simple text messages shown on the login page.
- **Payment Page Announcements**: Simple text messages shown on the payment page.

Actions you can perform:
- **fetch**: Retrieve the current announcement banner.
- **add**: Create a new announcement banner.
- **update**: Change the content of an existing announcement banner.
- **remove**: Remove an announcement banner.

For add and update actions, a description is required. Emojis are allowed in the banner content.

The tool updates both login page and payment page announcements simultaneously with the same content.
All announcements are formatted with HTML styling for consistent appearance.""",
    properties={
        "action": {
            "type": "string",
            "enum": [a.value for a in BannerAction],
            "description": "The operation to perform on the announcement banner: 'fetch' retrieves the current banner, 'add' creates a new banner, 'update' changes an existing banner's content, and 'remove' deletes the banner.",
        },
        "description": {
            "type": "string",
            "description": "The content of the banner. Required when creating or updating a banner. Emojis are allowed.",
        },
    },
    required=["action"],
)

standard_tools = [manage_announcement_banner_function]
tool_functions = {"manage_announcement_banner": manage_announcement_banner}
tools = ToolsSchema(standard_tools=standard_tools)
