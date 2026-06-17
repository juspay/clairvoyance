import json
from datetime import datetime
from typing import Any, Dict

from pipecat.frames.frames import EndFrame

from app.ai.voice.agents.breeze_buddy.callbacks import (
    service_callback,
)
from app.ai.voice.agents.breeze_buddy.observability.tracing_setup import (
    update_span_with_evaluation_data,
)
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.utils.hold_transfer import (
    publish_hold_transfer_result,
    summarize_transcription,
)
from app.core.logger import logger
from app.core.logger.context import clear_log_context
from app.database.accessor.breeze_buddy.chat_session import (
    flip_chat_session_to_chat,
)

callback_map = {
    "service_callback": service_callback,
}


async def end_conversation(context: TemplateContext, args, transition_to=None):
    """
    End the conversation by finalizing the call.

    This handler:
    1. Collects transcription and call data
    2. Calls all registered end_conversation_callbacks
    3. Hangs up the call
    4. Updates database with final call details
    5. Sends EndFrame to gracefully terminate the pipeline

    Returns:
        Empty dict
    """
    logger.info(
        f"End conversation handler called for call {context.call_sid} - finalizing call"
    )

    if context.conversation_ended:
        logger.info(
            f"Conversation already ended for call {context.call_sid}, skipping finalization"
        )
        return {}

    context.conversation_ended = True
    logger.debug(f"Set conversation_ended flag to True for call {context.call_sid}")

    # Initialize metaData if None to prevent crashes on metaData[...] writes
    # This handles cases where lead comes from DB with NULL meta_data column
    if context.lead and context.lead.metaData is None:
        context.lead.metaData = {}
        logger.debug(f"Initialized empty metaData for call {context.call_sid}")

    try:
        # ── Set call_ended_by default ──────────────────────────────────────
        # _handle_unexpected_disconnect already sets "customer"/"system"/"agent"
        # before calling end_conversation; this default only fires for normal
        # LLM-driven completions where nothing has set it yet.
        if "call_ended_by" not in context.lead.metaData:
            context.lead.metaData["call_ended_by"] = "agent"
            logger.debug(
                f"Set call_ended_by to 'agent' for normal flow completion in call {context.call_sid}"
            )

        # Collect transcription
        transcription = []
        filtered_transcript = []
        if context.context:
            history = context.context.messages
            logger.debug(
                f"Collecting transcription from {len(history)} messages for call {context.call_sid}"
            )

            for msg in history:
                if (
                    isinstance(msg, dict)
                    and "role" in msg
                    and "content" in msg
                    and isinstance(msg["content"], str)
                ):
                    # Skip Pipecat internal async-tool protocol messages
                    # (injected as user-role JSON blobs to coordinate async tool calls)
                    try:
                        parsed = json.loads(msg["content"])
                        if (
                            isinstance(parsed, dict)
                            and parsed.get("type") == "async_tool"
                        ):
                            continue
                    except (json.JSONDecodeError, TypeError):
                        pass

                    transcription.append(
                        {"role": msg["role"], "content": msg["content"]}
                    )
                    if msg["role"] in ("user", "assistant"):
                        filtered_transcript.append(
                            {"role": msg["role"], "content": msg["content"]}
                        )

            context.lead.metaData["transcription"] = transcription
            logger.info(
                f"Collected {len(transcription)} total messages "
                f"({len(filtered_transcript)} user/assistant) for call {context.call_sid}"
            )

        else:
            logger.warning(
                f"No context found for transcription collection in call {context.call_sid}"
            )

        # ── Widget voice attachment: flip channel back to CHAT ─────────────
        # In the voice-as-chat architecture the chat brain (run_chat_turn)
        # wrote every turn to the chat_session LIVE — there is no transcript to
        # drain and no agent_state to merge back. All that remains is to flip
        # current_channel VOICE → CHAT so /message and /voice/connect unblock.
        # This is the authoritative flip (the pipeline has fully torn down, so
        # no bridge turn is still writing); the /voice/end route's flip is the
        # crash safety net. Both go through the conditional UPDATE, so whichever
        # runs second is a no-op. Best-effort — a failure here must NOT block
        # the rest of end_conversation (DB lead update, callbacks, EndFrame).
        widget_session_id = context.lead.metaData.get("widget_session_id")
        if widget_session_id:
            try:
                # Drain the voice bridge FIRST so its in-flight turn (and the
                # per-session Redis lock it holds) is fully released before the
                # flip — otherwise a flip to CHAT could let a new /message turn
                # race the bridge's still-writing turn on the chat_message idx.
                bridge = getattr(context.bot, "_voice_bridge", None)
                if bridge is not None:
                    try:
                        await bridge.aclose()
                    except Exception as drain_err:  # noqa: BLE001
                        logger.warning(
                            f"widget voice: bridge drain failed for "
                            f"{widget_session_id}: {drain_err} (continuing)"
                        )
                await flip_chat_session_to_chat(
                    session_id=str(widget_session_id),
                    voice_lead_id=str(context.lead.id),
                )
                logger.info(
                    f"widget voice: flipped chat_session {widget_session_id} "
                    "back to CHAT on call end"
                )
            except Exception as flip_err:
                logger.error(
                    f"widget voice: flip-to-chat failed for chat_session "
                    f"{widget_session_id} / lead {context.lead.id}: {flip_err}",
                    exc_info=True,
                )

        # ── Hold-transfer: publish outbound result to inbound pod ──────────
        # Runs regardless of whether context.context exists so that outbound
        # legs that crash before LLM history is built still notify the waiting
        # hold_and_consult() instead of letting it time out.
        payload = context.lead.payload or {}
        pub_channel = payload.get("_hold_transfer_pub_channel")
        if pub_channel:
            try:
                # Determine status: "success" if LLM/agent ended the call,
                # "incomplete" if the customer hung up mid-way.
                call_ended_by = context.lead.metaData.get("call_ended_by")
                if call_ended_by == "customer":
                    status = "incomplete"
                elif call_ended_by:
                    status = "success"
                else:
                    # No explicit ender — unexpected disconnect
                    status = "incomplete"

                result_payload: Dict[str, Any] = {"status": status}

                if payload.get("_hold_transfer_summarize"):
                    reason = payload.get("_hold_transfer_reason", "")
                    summary = await summarize_transcription(filtered_transcript, reason)
                    if summary:
                        result_payload["summary"] = summary
                    else:
                        result_payload["transcription"] = filtered_transcript
                else:
                    result_payload["transcription"] = filtered_transcript

                await publish_hold_transfer_result(pub_channel, result_payload)
                logger.info(
                    f"[hold_transfer] Published outbound result for call "
                    f"{context.call_sid} with status={status}"
                )
            except Exception as pub_error:
                logger.error(
                    f"[hold_transfer] Failed to publish result for call "
                    f"{context.call_sid}: {pub_error}",
                    exc_info=True,
                )

        # Finalize the last node in node_traversal
        if (
            "node_traversal" in context.lead.metaData
            and context.lead.metaData["node_traversal"]
        ):
            context.record_node_exit()
            # Mark the last node as exited via "call_ended"
            last_entry = context.lead.metaData["node_traversal"][-1]
            if last_entry.get("via_function") is None:
                last_entry["via_function"] = "call_ended"

            logger.info(
                f"Finalized node traversal tracking for call {context.call_sid} - "
                f"{len(context.lead.metaData['node_traversal'])} nodes visited"
            )

        # Store errors collected during the call
        context.lead.metaData["errors"] = context.bot.errors

        # Update database first so the span picks up the final outcome from the DB response
        # (fire-and-forget outcome updates from update_outcome handler may not have completed yet)
        # For Daily mode: use lead.id (no telephony call_sid exists)
        # For telephony: use call_sid (how completion_function looks up the lead)
        is_daily_mode = getattr(context.bot, "transport_type", None) == "daily"

        if is_daily_mode and context.lead:
            # Daily mode: update by lead.id
            logger.info(
                f"Updating database with call completion details for lead {context.lead.id}"
            )
            updated_lead = await context.completion_function(
                call_id=context.lead.id,
                outcome=context.lead.outcome,
                call_end_time=datetime.now(),
                meta_data=context.lead.metaData,
            )
            if updated_lead:
                context.lead = updated_lead
            logger.info(f"Successfully updated database for lead {context.lead.id}")
        elif context.call_sid:
            # Telephony mode: update by call_sid (original behavior)
            logger.info(
                f"Updating database with call completion details for call {context.call_sid}"
            )
            updated_lead = await context.completion_function(
                call_id=context.call_sid,
                outcome=context.lead.outcome,
                call_end_time=datetime.now(),
                meta_data=context.lead.metaData,
            )
            if updated_lead:
                context.lead = updated_lead
            logger.info(f"Successfully updated database for call {context.call_sid}")
        else:
            logger.warning("No call_sid or lead found, skipping database update")

        # Update OpenTelemetry span with evaluation data AFTER DB write,
        # so context.lead.outcome reflects the final persisted value.
        update_span_with_evaluation_data(context)

        # Execute end_conversation_callbacks
        if context.end_conversation_callbacks:
            logger.info(
                f"Executing {len(context.end_conversation_callbacks)} end_conversation callbacks for call {context.call_sid}"
            )
            for callback_name in context.end_conversation_callbacks:
                try:
                    callback_handler = callback_map.get(callback_name)
                    if callback_handler:
                        logger.info(
                            f"Calling callback '{callback_name}' for call {context.call_sid}"
                        )
                        await callback_handler(context, args)
                        logger.info(
                            f"Successfully executed callback '{callback_name}' for call {context.call_sid}"
                        )
                    else:
                        logger.warning(
                            f"Callback handler '{callback_name}' not found in callback_map for call {context.call_sid}"
                        )
                except Exception as callback_error:
                    logger.error(
                        f"Error executing callback '{callback_name}' for call {context.call_sid}: {callback_error}",
                        exc_info=True,
                    )
        else:
            logger.debug(
                f"No end_conversation_callbacks configured for call {context.call_sid}"
            )

    except Exception as e:
        logger.error(
            f"Failed to finalize call {context.call_sid}: {str(e)}",
            exc_info=True,
        )
    finally:
        # Deny any pending HITL approvals before tearing down — pipecat does
        # NOT cancel parallel function-call tasks on EndFrame, so a blocked
        # gated handler would otherwise outlive the pipeline until timeout.
        approval_manager = getattr(context.bot, "approval_manager", None)
        if approval_manager:
            approval_manager.deny_all("conversation_ended")

        # Send EndFrame to gracefully terminate the pipeline
        logger.info(
            f"Sending EndFrame to terminate pipeline for call {context.call_sid}"
        )
        await context.task.queue_frame(EndFrame())
        logger.info(f"EndFrame queued for call {context.call_sid}")

        # Clear log context AFTER all logs to prevent leakage between calls
        clear_log_context()

    return {}
