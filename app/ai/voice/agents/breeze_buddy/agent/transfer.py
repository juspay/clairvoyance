"""Agent-to-agent transfer: apply a pending transfer between pipeline generations.

Extracted from ``Agent`` so the transfer swap/rebuild lives in one place. Called by
``Agent.run``'s generation loop when a ``connect_to_agent`` transfer is pending: it
swaps the active template and rebuilds the per-template inputs (flow builder, VAD, a
fresh transport over the same websocket), preserving the connection and the
accumulating call record. Behavior is identical to the former ``Agent._apply_transfer``.
"""

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, cast

from fastapi import WebSocket
from pipecat.runner.utils import _create_telephony_transport, create_transport

from app.ai.voice.agents.breeze_buddy.agent.transport import get_transport_params
from app.ai.voice.agents.breeze_buddy.guardrails.config import (
    load_guardrail_config,
)
from app.ai.voice.agents.breeze_buddy.template.builder import FlowConfigBuilder
from app.ai.voice.agents.breeze_buddy.template.context import (
    TemplateContext,
    with_context,
)
from app.ai.voice.agents.breeze_buddy.template.vad import create_vad_analyzer
from app.ai.voice.agents.breeze_buddy.utils.agent_transfer import PendingAgentTransfer
from app.core.config.dynamic import BB_DAILY_AUDIO_OUT_10MS_CHUNKS
from app.core.logger.context import update_log_context
from app.database.accessor.breeze_buddy.lead_call_tracker import update_lead_template

if TYPE_CHECKING:
    from app.ai.voice.agents.breeze_buddy.agent import Agent


async def apply_transfer(bot: "Agent", transfer: PendingAgentTransfer) -> None:
    """Swap to the target template and reset per-generation state.

    Preserves the connection (ws / call identity) and the accumulating call record
    (lead + metaData, agent_state, errors, conversation_id); rebuilds everything
    per-template (flow builder, VAD, a FRESH transport over the same websocket).
    conversation_ended stays False.
    """
    bot.transfer_count += 1
    bot.generation += 1

    # Close the outgoing generation's current node (e.g. the first template's
    # __direct__ node, which carries the connect_to_agent call) so node_traversal
    # shows a completed phase rather than a node left open.
    transfer_ctx = TemplateContext(bot)
    transfer_ctx.record_node_exit()

    # 1. Snapshot the outgoing generation's transcript (context still alive).
    if bot.context:
        bot.prior_generation_messages.extend(
            m
            for m in bot.context.messages
            if isinstance(m, dict) and isinstance(m.get("content"), str)
        )
        bot.prior_generation_messages.append(
            {
                "role": "system",
                "content": (
                    "[call transferred to agent template "
                    f"'{transfer.template.name}']"
                ),
            }
        )

    # Same for the outgoing generation's pipecat metrics — build_pipeline
    # replaces the collector on the next generation, so drain it now.
    if bot.metrics_collector:
        bot.prior_generation_metrics.extend(bot.metrics_collector.get_metrics())
        bot.metrics_collector = None

    # 2. Record + persist the transfer (same precedent as IVR selection).
    if bot.lead is not None:
        if bot.lead.metaData is None:
            bot.lead.metaData = {}
        bot.lead.metaData.setdefault("agent_transfers", []).append(
            {
                "from_template": bot.template.name if bot.template else None,
                "to_template": transfer.template.name,
                "from_template_id": (str(bot.template.id) if bot.template else None),
                "to_template_id": str(transfer.template.id),
                "switched_at": transfer_ctx._get_ist_timestamp(),
                "at": datetime.now(timezone.utc).isoformat(),
                "generation": bot.generation,
            }
        )
        await update_lead_template(
            lead_id=bot.lead.id,
            template=transfer.template.name,
            template_id=str(transfer.template.id),
        )

    # 3. Swap the template trio — everything downstream reads these.
    bot.template = transfer.template
    bot.configurations = transfer.template.configurations
    # The outgoing coordinator is bound to the previous template's rules and
    # model services. Clear it before loading the replacement configuration so
    # future reordering cannot expose the old policy during the swap.
    bot.guardrail_coordinator = None
    bot.guardrails = await load_guardrail_config(
        str(transfer.template.id),
        bot.configurations,
        supported_channels=list(transfer.template.supported_channels),
    )
    bot.template_vars = transfer.template_vars
    bot._handoff_messages = transfer.handoff_messages

    # 4. Rebuild per-template inputs the same way setup does.
    bot.flow_builder = FlowConfigBuilder()
    for name, fn in bot.flow_builder.handler_map.items():
        bot.flow_builder.handler_map[name] = with_context(bot)(fn)
    bot.vad_analyzer, bot.default_vad_params = await create_vad_analyzer(
        is_daily_mode=bot.is_daily_mode,
        template=bot.template,
    )

    # 5. FRESH transport over the SAME connection. pipecat transports latch
    #    _initialized and cannot restart — a new instance around the same ws
    #    resets state; the NonClosingWebSocket proxy keeps the socket alive.
    if bot.is_daily_mode:
        transport_params = get_transport_params(
            bot.template,
            bot.configurations,
            daily_audio_out_10ms_chunks=await BB_DAILY_AUDIO_OUT_10MS_CHUNKS(),
        )
        bot.transport = await create_transport(
            bot._rebuild.runner_args, transport_params
        )
        # Keep-alive: reuse the SAME joined DailyTransportClient instead of the
        # fresh one create_transport just built. A fresh client would join a new
        # meeting session (new Meeting ID) and orphan the browser client, which
        # never left the old session. The client's event callbacks are bound to
        # the transport that created them, so re-point the preserved client's
        # callbacks at the new transport (grabbed from the fresh, never-joined
        # client before discarding it). Set _client before build_pipeline calls
        # input()/output() so they wrap the preserved client.
        if bot._daily_client is not None:
            # bot.transport is typed BaseTransport here (create_transport's
            # return); the DailyTransportClient _client swap is intentionally
            # dynamic, so route it through an Any local.
            daily_transport: Any = bot.transport
            fresh_client = daily_transport._client
            daily_transport._client = bot._daily_client
            bot._daily_client._callbacks = fresh_client._callbacks
    else:
        # Set during the first _setup_telephony_transport; always present here.
        assert bot._rebuild.telephony_transport_type is not None
        assert bot._rebuild.telephony_call_data is not None
        # Telephony doesn't use the Daily chunk knob; leave the fallback.
        transport_params = get_transport_params(bot.template, bot.configurations)
        params = transport_params[bot._rebuild.telephony_transport_type]()
        bot.transport = await _create_telephony_transport(
            cast(WebSocket, bot._rebuild.ws_proxy),
            params,
            bot._rebuild.telephony_transport_type,
            bot._rebuild.telephony_call_data,
        )

    # 6. Reset per-generation runtime state. conversation_ended stays False;
    #    lead/agent_state/errors/conversation_id persist across generations.
    bot.task = None
    bot.context = None
    bot.flow_manager = None
    bot._context_aggregator = None
    bot._rtvi_processor = None
    bot.approval_manager = None
    bot._user_idle_callback_handler = None
    bot._flow_initialized = False
    bot.greeting_source = None  # → respond_immediately=True for gen >= 2:
    bot.greeting_text = None  # the new agent speaks first
    if bot._post_greeting_task and not bot._post_greeting_task.done():
        bot._post_greeting_task.cancel()
        bot._post_greeting_task = None
    update_log_context(generation=str(bot.generation))
