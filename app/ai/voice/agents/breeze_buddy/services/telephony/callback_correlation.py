"""Provider-neutral callback correlation for outbound call placement."""

from __future__ import annotations

from typing import Any, Mapping

from app.ai.voice.agents.breeze_buddy.services.telephony.plivo.correlation import (
    bind_plivo_outbound_call_uuid,
    plivo_callback_context,
)
from app.schemas import CallProvider


async def bind_outbound_call_identity(
    *,
    provider: str | CallProvider,
    params: Mapping[str, Any],
    call_id: str | None,
) -> bool:
    """Bind a provider callback's real call id to its pending outbound lead.

    Most providers return the authoritative call id synchronously from
    ``make_call``. Plivo can initially return only ``request_uuid``, then sends
    ``CallUUID`` on answer/status callbacks. Keep that provider-specific
    binding detail behind this adapter so routers do not import Plivo
    correlation internals directly.
    """
    provider_value = provider.value if isinstance(provider, CallProvider) else provider
    if provider_value.lower() != CallProvider.PLIVO.value.lower():
        return False

    lead_id, telephony_number_id = plivo_callback_context(params)
    return await bind_plivo_outbound_call_uuid(
        lead_id=lead_id,
        call_uuid=call_id,
        telephony_number_id=telephony_number_id,
    )
