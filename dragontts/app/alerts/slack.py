"""Slack alerting — incoming-webhook Block Kit sender.

Mirrors clairvoyance's ``app/services/slack/alert.py`` contract: an ``Alert``
singleton (``slack_alert``) that POSTs a Block Kit message to
``SLACK_WEBHOOK_URL`` and treats HTTP 200 + body ``ok`` as success. It **never
raises** — a Slack outage logs + returns ``False``; it can never break the
request path or the background summary task. An empty webhook URL makes
``send()`` a safe no-op (== feature off), matching clairvoyance (no separate
enable flag).

Uses httpx (already a DragonTTS dependency) rather than clairvoyance's aiohttp;
the on-wire Block Kit payload is equivalent.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import logger


def _format_tag(user: str) -> str:
    """Wrap a handle as a Slack mention, unless it's already a Slack tag form
    (``<@U123>`` user or ``<!subteam^ID|@team>`` group — pass those through)."""
    u = user.strip()
    if not u:
        return ""
    if u.startswith("<") and u.endswith(">"):
        return u
    return f"<@{u}>"


class Alert:
    """Incoming-webhook Block Kit sender. Best-effort: never raises."""

    def __init__(
        self, webhook_url: str | None = None, tag_users: str | None = None
    ) -> None:
        self.webhook_url = (
            webhook_url if webhook_url is not None else settings.slack_webhook_url
        )
        self.tag_users = (
            tag_users if tag_users is not None else settings.slack_tag_users
        )

    def _blocks(
        self,
        title: str,
        fields,
        sections,
        links,
        include_tags: bool,
    ) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": title, "emoji": True},
            },
        ]
        if fields:
            # One section; Slack renders its fields in 2 columns.
            blocks.append(
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*{f['name']}:*\n{f['value']}"}
                        for f in fields
                    ],
                }
            )
        # Each section is its own block — a plain mrkdwn string, or a
        # {title/name, text} dict rendered as a bold sub-heading + body
        # (mirrors clairvoyance's summary sections).
        for s in sections or []:
            if isinstance(s, str):
                text = s
            else:
                head = s.get("title") or s.get("name", "")
                text = f"*{head}:*\n{s.get('text', '')}" if head else s.get("text", "")
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
        for link in links or []:
            url = link.get("url", "")
            name = link.get("name", link.get("text", "Link"))
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"🔗 <{url}|{name}>"},
                }
            )
        if include_tags and self.tag_users:
            tags = " ".join(
                t for t in (_format_tag(u) for u in self.tag_users.split(",")) if t
            )
            if tags:
                blocks.append(
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"cc: {tags}"},
                    }
                )
        return blocks

    async def send(
        self,
        title: str,
        fields=None,
        sections=None,
        links=None,
        fallback_text: str | None = None,
        include_tags: bool = True,
    ) -> bool:
        """POST a Block Kit message. Returns True on success, False otherwise.

        Never raises — Slack down/timeout/misconfigured all log + return False.
        """
        if not self.webhook_url:
            logger.warning("Slack webhook URL not configured — alert skipped")
            return False
        try:
            # Build the payload inside the guard too — _blocks()/formatting can
            # raise on a malformed field/section, and send() must never raise.
            message = {
                "blocks": self._blocks(title, fields, sections, links, include_tags),
                "text": fallback_text or title,  # notification preview text
            }
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    self.webhook_url,
                    json=message,
                    headers={"Content-Type": "application/json"},
                )
            body = (resp.text or "").strip().lower()
            if resp.status_code == 200 and body == "ok":
                return True
            logger.error(
                f"Slack webhook non-ok: status={resp.status_code} body={resp.text[:200]}"
            )
            return False
        except Exception as e:  # noqa: BLE001 — best-effort, never raise
            logger.error(f"Error sending Slack alert: {e}")
            return False


# Module-level singleton — callers do `await slack_alert.send(...)`.
slack_alert = Alert()
