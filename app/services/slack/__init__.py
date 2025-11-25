"""
Slack Services Module

Provides Slack webhook integration for sending alerts and notifications.
"""

from .alert import Alert, slack_alert

__all__ = [
    "slack_alert",
]
