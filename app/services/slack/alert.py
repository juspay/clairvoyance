"""
Slack Alert Integration

This module provides functionality to send alerts to Slack via webhooks.
"""

from typing import Any, Dict, List, Optional

import aiohttp

from app.core.config.static import (
    SLACK_TAG_USERS,
    SLACK_WEBHOOK_URL,
)
from app.core.logger import logger


class Alert:
    """Send alerts to Slack via webhook"""

    def __init__(self):
        self.webhook_url = SLACK_WEBHOOK_URL

    async def send(
        self,
        title: str,
        fields: Optional[List[Dict[str, str]]] = None,
        sections: Optional[List[Dict[str, str]]] = None,
        links: Optional[List[Dict[str, str]]] = None,
        fallback_text: Optional[str] = None,
        include_tags: bool = True,
    ) -> bool:
        """
        Generic function to send Slack alerts with customizable content.

        Args:
            title: Alert title/header (displayed with emoji)
            fields: Optional list of field dicts with 'name' and 'value' keys
            sections: Optional list of section dicts with 'title' and 'text' keys
            links: Optional list of link dicts with 'text' and 'url' keys
            fallback_text: Optional fallback text for notifications (defaults to title)
            include_tags: Whether to include @mention tags (default True).
                Set to False to suppress tagging and reduce Slack notification noise.

        Returns:
            True if sent successfully, False otherwise
        """
        if not self.webhook_url:
            logger.warning("Slack webhook URL not configured")
            return False

        try:
            # Build blocks - use Any type for flexible Slack block structure
            blocks: List[Dict[str, Any]] = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": title,
                        "emoji": True,
                    },
                }
            ]

            # Add fields section if provided
            if fields:
                # Slack fields are displayed in 2 columns, so we group them
                field_items = []
                for field in fields:
                    field_items.append(
                        {
                            "type": "mrkdwn",
                            "text": f"*{field.get('name', '')}:*\n{field.get('value', '')}",
                        }
                    )

                blocks.append({"type": "section", "fields": field_items})

            # Add custom sections if provided
            if sections:
                for section in sections:
                    section_block = {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*{section.get('title', '')}:*\n{section.get('text', '')}",
                        },
                    }
                    blocks.append(section_block)

            # Add links if provided
            if links:
                for link in links:
                    blocks.append(
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"🔗 <{link.get('url', '')}|{link.get('text', 'Link')}>",
                            },
                        }
                    )

            # Add notifications section if users are configured for tagging
            if include_tags and SLACK_TAG_USERS:
                # Parse comma-separated usernames and filter out empty ones
                users = [
                    user.strip() for user in SLACK_TAG_USERS.split(",") if user.strip()
                ]
                if users:
                    # Format users as proper Slack mentions
                    mentions = []
                    for user in users:
                        # Check if it's already a complete Slack mention format
                        # (e.g., <!subteam^ID|@team-name> for user groups or <@U12345> for users)
                        if user.startswith("<") and user.endswith(">"):
                            # Already formatted, use as-is
                            mentions.append(user)
                        else:
                            # Remove @ if present and wrap in Slack user mention format
                            clean_user = user.lstrip("@")
                            mentions.append(f"<@{clean_user}>")

                    # Join mentions with spaces
                    mentions_text = " ".join(mentions)

                    # Add notifications section
                    blocks.append(
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"cc: {mentions_text}",
                            },
                        }
                    )

            # Build final message
            message = {
                "blocks": blocks,
                "text": fallback_text or title,  # Fallback text for notifications
            }

            # Log outgoing request
            logger.info(f"Sending Slack alert: {title}")

            # Send to Slack with timeout to prevent hanging
            timeout = aiohttp.ClientTimeout(total=30)  # 30 second timeout
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self.webhook_url,
                    json=message,
                    headers={"Content-Type": "application/json"},
                ) as response:
                    response_text = await response.text()

                    # Log response details
                    logger.info(
                        f"Slack API response - Status: {response.status}, "
                        f"Body: {response_text}"
                    )

                    # Slack webhooks return "ok" on success
                    if response.status == 200 and response_text.lower() == "ok":
                        logger.info(f"Slack alert sent successfully: {title}")
                        return True
                    else:
                        logger.error(
                            f"Failed to send Slack alert - Status: {response.status}, "
                            f"Response: {response_text}, Title: {title}"
                        )
                        return False

        except Exception as e:
            logger.error(f"Error sending Slack alert: {e}")
            return False


# Global instance
slack_alert = Alert()
