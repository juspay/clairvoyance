import json
from datetime import datetime, timezone

from pipecat.frames.frames import EndFrame

from app.ai.voice.agents.breeze_buddy.callbacks import (
    service_callback,
)
from app.ai.voice.agents.breeze_buddy.observability.tracing_setup import (
    update_span_with_evaluation_data,
)
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.core.logger import logger

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

        # Update OpenTelemetry span with comprehensive evaluation data for LLM-as-a-Judge
        update_span_with_evaluation_data(context)

        # Hangup call
        if context.hangup_function:
            logger.info(f"Calling hangup_function for call {context.call_sid}")
            context.hangup_function(context.call_sid)
            logger.info(f"Successfully hung up call {context.call_sid}")
        else:
            logger.warning(f"No hangup_function available for call {context.call_sid}")

        # Update database
        if context.call_sid:
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
            logger.warning("No call_sid found, skipping database update")

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

    return {}
