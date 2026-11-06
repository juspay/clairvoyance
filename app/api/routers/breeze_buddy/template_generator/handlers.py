"""Business-logic handler for the template generator chat endpoint.

Thin layer between the FastAPI route and ``TemplateGeneratorService`` — its
only job is to translate the validated ``GenerateChatRequest`` into the
service's format and return a ``StreamingResponse``.

Not separated into a heavier handler the way the templates router is because
there is no DB access, no lock management, and no state — just a straight
pipe from Claude to the client.
"""

from __future__ import annotations

from fastapi.responses import StreamingResponse

from app.ai.voice.agents.breeze_buddy.template.generator.service import (
    TemplateGeneratorService,
)
from app.schemas import UserInfo
from app.schemas.breeze_buddy.template_generator import GenerateChatRequest


async def generate_chat_handler(
    req: GenerateChatRequest, current_user: UserInfo
) -> StreamingResponse:
    """Stream a Claude template-generation / refinement turn.

    Converts ``req.messages`` to the Anthropic dict format and delegates to
    ``TemplateGeneratorService.stream()``.

    Returns:
        ``StreamingResponse`` with ``Content-Type: text/event-stream``.
        The stream yields events in this order:

        - ``event: template_start`` (once) — when Claude opens a ```json fence
        - ``event: delta`` — text chunk (``data.text``)
        - ``event: error`` — on failure (``data.code``, ``data.message``)
        - ``event: done`` — always last (``data.template``: extracted JSON str or null)
    """
    messages = [{"role": m.role, "content": m.content} for m in req.messages]

    # Prefer the ids the console already resolved (workspace picked in the
    # wizard) over the JWT lists: admin JWTs carry ['*'] wildcards, which
    # make the service prompt tell Claude to ASK the user which
    # reseller_id/merchant_id to use — a question the console has already
    # answered. Request ids only shape the generated JSON; RBAC is enforced
    # at template create, so this is context, not an access decision.
    reseller_ids = (
        [req.reseller_id]
        if req.reseller_id and req.reseller_id != "*"
        else current_user.reseller_ids
    )
    merchant_ids = (
        [req.merchant_id]
        if req.merchant_id and req.merchant_id != "*"
        else current_user.merchant_ids
    )

    service = TemplateGeneratorService(
        messages=messages,
        reseller_ids=reseller_ids,
        merchant_ids=merchant_ids,
        current_template=req.current_template,
    )

    return StreamingResponse(
        service.stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
