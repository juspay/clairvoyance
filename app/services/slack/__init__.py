"""
Slack Services Module

Provides Slack webhook integration for sending alerts and notifications.
"""

from .alert import slack_alert

__all__ = [
    "slack_alert",
]
