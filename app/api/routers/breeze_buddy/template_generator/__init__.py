"""Template generator router — AI-assisted Breeze Buddy template authoring.

Single endpoint:

    POST /templates/ai/chat

    Accepts a ``GenerateChatRequest`` (conversation history + optional
    current template for refinement) and streams Server-Sent Events back.

    The endpoint is intentionally stateless — it does NOT save anything.
    After reviewing the generated JSON the caller uses ``POST /templates``
    to persist it.

Authentication:
    Requires a valid RBAC token (``get_current_user_with_rbac``).
    Any authenticated user (admin, reseller, merchant) may call this
    endpoint; generation is purely read-side and creates no DB rows.

SSE event taxonomy:
    event: template_start  — Claude opened a ```json code fence
    event: delta           — text chunk  (data: {"text": "..."})
    event: error           — failure     (data: {"code": "...", "message": "..."})
    event: done            — always last (data: {"template": "<json str or null>"})
"""

from fastapi import APIRouter, Depends

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo
from app.schemas.breeze_buddy.template_generator import GenerateChatRequest

from .handlers import generate_chat_handler

router = APIRouter()


@router.post(
    "/templates/ai/chat",
    summary="AI-assisted template generation / refinement (streaming SSE)",
    response_description=(
        "Server-Sent Events stream. " "See module docstring for event taxonomy."
    ),
)
async def generate_chat(
    req: GenerateChatRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Stream a Claude template-generation or refinement conversation turn.

    Pass the full conversation history in ``messages`` (alternating
    user / assistant turns — same convention as the Anthropic messages API).
    For refinement, include the existing template in ``current_template``.

    The endpoint streams SSE events until Claude finishes:

    - ``template_start`` — fired once when Claude begins emitting a JSON
      code fence; the UI should open the side-by-side preview pane.
    - ``delta`` — one text chunk (``data.text``).
    - ``error`` — on failure (``data.code``, ``data.message``); always
      followed by ``done``.
    - ``done`` — final event; ``data.template`` is the extracted raw JSON
      string if Claude produced one, otherwise ``null``.

    The endpoint does NOT save the template. Use ``POST /templates`` to
    persist the result after user review.
    """
    return await generate_chat_handler(req, current_user)


__all__ = ["router"]
