import json
from datetime import datetime

from pipecat.frames.frames import EndFrame

from app.ai.voice.agents.breeze_buddy.callbacks import (
    service_callback,
)
from app.ai.voice.agents.breeze_buddy.observability.tracing_setup import (
    update_span_with_evaluation_data,
)
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.core.logger import logger
from app.core.logger.context import clear_log_context

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

        # Set call_ended_by if not already set (e.g., by _handle_unexpected_disconnect)
        if "call_ended_by" not in context.lead.metaData:
            # Default to "agent" for normal conversation flow completion
            context.lead.metaData["call_ended_by"] = "agent"
            logger.debug(
                f"Set call_ended_by to 'agent' for normal flow completion in call {context.call_sid}"
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

        # Update OpenTelemetry span with comprehensive evaluation data for LLM-as-a-Judge
        update_span_with_evaluation_data(context)

        # Update database
        # For Daily mode: use lead.id (no telephony call_sid exists)
        # For telephony: use call_sid (how completion_function looks up the lead)
        is_daily_mode = getattr(context.bot, "transport_type", None) == "daily"

        if is_daily_mode and context.lead:
            # Daily mode: update by lead.id
            logger.info(
                f"Updating database with call completion details for lead {context.lead.id}"
            )
            context.lead = await context.completion_function(
                call_id=context.lead.id,
                outcome=context.lead.outcome,
                call_end_time=datetime.now(),
                meta_data=context.lead.metaData,
            )
            logger.info(f"Successfully updated database for lead {context.lead.id}")
        elif context.call_sid:
            # Telephony mode: update by call_sid (original behavior)
            logger.info(
                f"Updating database with call completion details for call {context.call_sid}"
            )
            context.lead = await context.completion_function(
                call_id=context.call_sid,
                outcome=context.lead.outcome,
                call_end_time=datetime.now(),
                meta_data=context.lead.metaData,
            )
            logger.info(f"Successfully updated database for call {context.call_sid}")
        else:
            logger.warning("No call_sid or lead found, skipping database update")

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
        # Send EndFrame to gracefully terminate the pipeline
        logger.info(
            f"Sending EndFrame to terminate pipeline for call {context.call_sid}"
        )
        await context.task.queue_frame(EndFrame())
        logger.info(f"EndFrame queued for call {context.call_sid}")

        # Clear log context AFTER all logs to prevent leakage between calls
        clear_log_context()

    return {}
