"""Wire contract for the parent -> child Daily bot launch payload.

Single source of truth for the JSON handed to ``bot_runner`` over stdin:
``daily.py`` serializes it, ``bot_runner`` parses it, so the two halves of
the process boundary cannot drift (and a field added on one side without the
other fails loudly instead of being silently dropped).

Deliberately imports nothing from the app: it must stay importable before
``bot_runner``'s ``load_dotenv()`` has run.
"""

from typing import Any, Dict

from pydantic import BaseModel, field_validator


class BotLaunchPayload(BaseModel):
    """The one JSON object written to the bot child's stdin."""

    room_url: str
    token: str
    body: Dict[str, Any]

    @field_validator("room_url", "token")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value:
            raise ValueError("must be non-empty")
        return value

    @field_validator("body")
    @classmethod
    def _has_lead_id(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        if not value.get("lead_id"):
            raise ValueError("must contain a truthy 'lead_id'")
        return value
