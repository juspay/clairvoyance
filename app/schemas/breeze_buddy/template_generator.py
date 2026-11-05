"""Request/response schemas for the template generator endpoints.

``POST /templates/ai/chat`` accepts ``GenerateChatRequest`` and streams
SSE events back. The endpoint does NOT persist — the caller uses
``POST /templates`` to save the resulting JSON.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class GenerateChatMessage(BaseModel):
    """A single turn in the template generation conversation.

    Mirrors the Anthropic messages API format so the service can pass
    the list directly without transformation.
    """

    role: Literal["user", "assistant"] = Field(
        description="Message author. 'user' for the human, 'assistant' for Claude."
    )
    content: str = Field(
        description="Message text.",
        min_length=1,
    )


class GenerateChatRequest(BaseModel):
    """Request body for ``POST /templates/ai/chat``.

    The caller maintains the full conversation history and sends it on
    every turn (same pattern as the Anthropic messages API). This keeps
    the endpoint stateless — no session storage needed.

    For a refinement session, pass ``current_template`` with the existing
    template JSON. Claude will use it as a starting point and the service
    will inject it as the first user message automatically.
    """

    messages: List[GenerateChatMessage] = Field(
        description=(
            "Full conversation history: alternating user/assistant turns. "
            "The latest entry must be a 'user' turn (the user's current message)."
        ),
        min_length=1,
    )
    reseller_id: str = Field(
        description=(
            "Reseller namespace for the template being generated. "
            "Included for context so Claude can embed it in the output JSON."
        )
    )

    merchant_id: Optional[str] = Field(
        default=None,
        description=(
            "Merchant (workspace) already chosen in the console for this "
            "template. When provided, Claude embeds it in the output JSON "
            "instead of asking the user which merchant to use."
        ),
    )

    current_template: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Existing template JSON when this is a refinement session. "
            "When provided, Claude receives the template as context and the "
            "system prompt switches to refinement mode. "
            "Leave null (default) for new-template generation."
        ),
    )
