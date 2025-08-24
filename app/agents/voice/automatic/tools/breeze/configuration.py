import json
from enum import Enum
from typing import Optional, Dict, Any

from app.core.logger import logger
from pipecat.services.llm_service import FunctionCallParams
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from .utils import (
    get_current_shop_config_data,
    patch_shop_config,
)

# These will be set by the initializer
breeze_token: str | None = None
shop_id: str | None = None
shop_url: str | None = None
merchant_id: str | None = None
user_id: str | None = None


class BannerType(str, Enum):
    LOGIN_PAGE_ANNOUNCEMENT = "loginPageAnnouncement"
    PAYMENT_PAGE_ANNOUNCEMENT = "paymentPageAnnouncement"

class Action(str, Enum):
    ADD = "add"
    UPDATE = "update"
    REMOVE = "remove"

async def create_announcement_banner(params: FunctionCallParams):
    """
    Manages banners by adding, updating, or removing them from the shop's configuration.
    
    This tool can handle:
    - Login Page Announcements: Simple text messages shown on the login page.
    - Payment Page Announcements: Simple text messages shown on the payment page.
    
    Actions you can perform:
    - add: Create a new announcement.
    - update: Change the content of an existing announcement.
    - remove: Delete an announcement.
    """
    try:
        # Default to loginPageAnnouncement if bannerType is not provided
        banner_type = params.arguments.get("bannerType", "loginPageAnnouncement")
        action = params.arguments.get("action")
        description = params.arguments.get("description")

        if not shop_id or not shop_url or not merchant_id or not breeze_token:
            logger.error("Banner tool called without required context (breezeToken, shopId, shopUrl, merchantId).")
            await params.result_callback({"success": False, "error": "Banner tool is not configured with shop information."})
            return

        logger.info(f"updateBanner started: {action} for {banner_type}")
        
        if (action == "add" or action == "update") and not description:
            await params.result_callback({"success": False, "error": "Description is required when adding or updating a banner."})
            return
        
        try:
            config = await get_current_shop_config_data(shop_url)
        except ValueError as e:
            logger.error(f"Failed to get shop configuration: {str(e)}")
            await params.result_callback({
                "success": False, 
                "error": f"Could not retrieve shop configuration: {str(e)}"
            })
            return
            
        config_to_patch = {**config}
        config_key = None

        if(banner_type == "paymentPageAnnouncement"):
            config_key = "announcementBannerText"
        elif(banner_type == "loginPageAnnouncement"):
            config_key = "loginPageAnnouncementText"


        if config_key is None:
            await params.result_callback({"success": False, "error": "Invalid banner type."})
            return

        if action == "add" or action == "update":
            config_to_patch[config_key] = description
            logger.info(f"Setting {config_key} to '{description}'")
        elif action == "remove":
            del config_to_patch[config_key]
            logger.info(f"Deleted {config_key}")
        
        if merchant_id == config.get("merchantId"):
            patch_result = await patch_shop_config(shop_url, user_id, config_to_patch, breeze_token)

            logger.info(f"Updated shop config for {shop_url} with changes to {config_key}")
            logger.info(f"Config changes: {json.dumps({banner_type: config_to_patch.get(config_key)})}")
            
            # If patch was successful, fetch and log the updated configuration
            if patch_result and patch_result.get("status") != "failure":
                try:
                    updated_config = await get_current_shop_config_data(shop_url)
                    logger.info(f"Updated shop configuration: {json.dumps(updated_config)}")
                except ValueError as e:
                    logger.error(f"Failed to fetch updated configuration: {e}")
            
            # Return the patch_result
            await params.result_callback(patch_result)
        else:
            await params.result_callback({"success": False, "error": "Merchant ID mismatch. Cannot update configuration."})
            return
        
    except Exception as e:
        error_message = str(e)
        logger.error(f"updateBanner error: {error_message}")
        await params.result_callback({"success": False, "error": error_message})

create_announcement_banner_function = FunctionSchema(
    name="create_announcement_banner",
    description="""Manages banners by adding, updating, or removing them from the shop's configuration.

This tool can handle:
- **Login Page Announcements**: Simple text messages shown on the login page.
- **Payment Page Announcements**: Simple text messages shown on the payment page.

Actions you can perform:
- **add**: Create a new announcement.
- **update**: Change the content of an existing announcement.
- **remove**: Delete an announcement.

For announcements, only a description is needed.

By default, if no banner type is specified, the tool will operate on the Login Page Announcement.""",
    properties={
        "bannerType": {
            "type": "string",
            "enum": [bt.value for bt in BannerType],
            "description": """The type of banner or message to manage.
- 'loginPageAnnouncement': A simple text message displayed on the login page. Good for general announcements. This is the default if not specified.
- 'paymentPageAnnouncement': A simple text message displayed on the payment page. Good for important notices during checkout. 
"""
        },
        "action": {
            "type": "string",
            "enum": [a.value for a in Action],
            "description": "The operation to perform. 'add' creates a new item. 'update' modifies an existing item. 'remove' deletes an item."
        },
        "description": {
            "type": "string",
            "description": "The content of the banner. Required when adding or updating any type of banner."
        }
    },
    required=["action"]
)

standard_tools = [create_announcement_banner_function]
tool_functions = {"create_announcement_banner": create_announcement_banner}
tools = ToolsSchema(standard_tools=standard_tools)
