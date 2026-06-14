"""
In-process audio bridge between a telephony WebSocket leg and a Daily room.

Used by the Daily warm-transfer flow: when the dialed agent picks up, the
telephony provider opens a WebSocket to the bridge endpoint. This module
builds a Pipecat dual-transport pipeline that forwards audio in both
directions with no STT/LLM/TTS in the path. Sample-rate conversion is
delegated to Pipecat's transport layer (input transport emits frames at its
native rate; output transport resamples to its own configured rate).

V1 supports Plivo only. Exotel works through the same dispatcher path and
can be added by wiring its serializer + provider hangup; Twilio requires a
separate dial path (its `make_call` does not route through `/answer`).
"""

from fastapi import WebSocket
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
)
from pipecat.pipeline.parallel_pipeline import ParallelPipeline
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.serializers.plivo import PlivoFrameSerializer
from pipecat.transports.daily.transport import DailyParams, DailyTransport
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from app.ai.voice.agents.breeze_buddy.template.vad import TELEPHONY_SAMPLE_RATE
from app.ai.voice.agents.breeze_buddy.utils.bridge_flag import (
    STATUS_DISCONNECTED,
    STATUS_FAILED,
    STATUS_JOINED,
    clear_bridge_flag,
    get_bridge_flag,
    update_bridge_status,
)
from app.core.config.static import PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN
from app.core.logger import logger
from app.database.accessor.breeze_buddy.outbound_number import (
    decrement_outbound_number_channels,
    update_outbound_number_status,
)
from app.schemas import OutboundNumberStatus

# Daily delivers audio to us at its preferred rate (16 kHz). Pipecat's SOXR
# resampler in FastAPIWebsocketTransport.output() then downsamples 16k→8k
# before sending to Plivo. Asking Daily SDK to do a 48k→8k downsample
# internally produces degraded audio; the 16k→8k step is cleaner via SOXR.
DAILY_BRIDGE_AUDIO_IN_SAMPLE_RATE = 16000

# Telephony audio going INTO Daily is 8 kHz. Daily's WebRTC layer handles the
# upsample internally, so no SOXR step is needed on this path.
DAILY_BRIDGE_AUDIO_OUT_SAMPLE_RATE = TELEPHONY_SAMPLE_RATE  # 8000
BRIDGE_BOT_NAME = "transfer-bridge"

# ---------------------------------------------------------------------------
# In-process bridge task registry
# Maps agent_call_id → PipelineTask for active bridge pipelines.
# Used by the telephony status callback to terminate a bridge when the
# agent's call ends (rather than waiting for the WebSocket to close).
# ---------------------------------------------------------------------------
_bridge_tasks: dict[str, PipelineTask] = {}


async def terminate_bridge(call_id: str) -> bool:
    """Queue an EndFrame on the named bridge pipeline.

    Returns True if a running bridge was found and signalled, False otherwise.
    Safe to call from any async context in the same process.
    """
    task = _bridge_tasks.get(call_id)
    if not task:
        return False
    from pipecat.frames.frames import EndFrame

    await task.queue_frame(EndFrame())
    logger.info(f"[BridgeRun] Termination signalled for call {call_id}")
    return True


class _AudioForwarder(FrameProcessor):
    """Converts incoming InputAudioRawFrame to OutputAudioRawFrame so it can
    be sent out on a different transport's output side. All other frames
    (StartFrame, EndFrame, SystemFrame, etc.) are passed through unchanged so
    downstream output transports receive the lifecycle frames they need to
    initialise their MediaSender.

    The optional label is used in periodic debug logs so audio flow can be
    confirmed from the logs without attaching a debugger.
    """

    def __init__(self, label: str = ""):
        super().__init__()
        self._label = label
        self._count = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            self._count += 1
            if self._count % 100 == 0:
                logger.debug(
                    f"[BridgeRun] {self._label} forwarded {self._count} frames"
                )
            await self.push_frame(
                OutputAudioRawFrame(
                    audio=frame.audio,
                    sample_rate=frame.sample_rate,
                    num_channels=frame.num_channels,
                ),
                direction,
            )
        else:
            await self.push_frame(frame, direction)


def _build_telephony_serializer(provider: str, stream_id: str, call_id: str):
    """Pipecat serializer for the dialed agent leg."""
    if provider == "plivo":
        return PlivoFrameSerializer(
            stream_id=stream_id,
            call_id=call_id,
            auth_id=PLIVO_AUTH_ID,
            auth_token=PLIVO_AUTH_TOKEN,
        )
    raise NotImplementedError(
        f"Bridge serializer not implemented for provider '{provider}'. "
        "V1 supports Plivo only."
    )


async def run_bridge(
    websocket: WebSocket,
    provider: str,
    stream_id: str,
    call_id: str,
    room_url: str,
    daily_token: str,
) -> None:
    """Build and run the bridge pipeline until either leg disconnects.

    Updates the Redis bridge flag as the bridge transitions through joined →
    disconnected (or failed) so the AI bot's wait loop can react.

    Args:
        websocket: the accepted FastAPI WebSocket from the telephony provider.
        provider: lowercase provider name ("plivo").
        stream_id: provider stream id parsed from the WS handshake.
        call_id: provider call sid (= bridge flag key).
        room_url: Daily room URL the AI bot is in.
        daily_token: fresh owner token minted by the Daily handler.
    """
    _failed = False  # tracks whether an exception path already set STATUS_FAILED

    try:
        # Build transports and pipeline inside the try block so that errors
        # here (e.g. NotImplementedError for unsupported providers) are caught,
        # published as STATUS_FAILED, and re-raised rather than leaving the
        # warm-transfer poller spinning until its timeout fires.
        serializer = _build_telephony_serializer(provider, stream_id, call_id)

        telephony_transport = FastAPIWebsocketTransport(
            websocket=websocket,
            params=FastAPIWebsocketParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,
                audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
                add_wav_header=False,
                serializer=serializer,
            ),
        )

        daily_transport = DailyTransport(
            room_url=room_url,
            token=daily_token,
            bot_name=BRIDGE_BOT_NAME,
            params=DailyParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_in_sample_rate=DAILY_BRIDGE_AUDIO_IN_SAMPLE_RATE,
                audio_out_sample_rate=DAILY_BRIDGE_AUDIO_OUT_SAMPLE_RATE,
            ),
        )

        @daily_transport.event_handler("on_joined")
        async def on_joined(transport, data):
            await update_bridge_status(call_id, STATUS_JOINED)
            logger.info(f"[BridgeRun] Bridge joined Daily room for call {call_id}")

        # Single pipeline with a ParallelPipeline so both directions share one
        # PipelineTask and one StartFrame sequence. The previous dual-Pipeline /
        # asyncio.gather approach caused both tasks to race over the shared
        # transport instances during startup (DailyTransport.start() is not
        # re-entrant), leaving both StartFrames permanently unacknowledged and
        # the bridge frozen with no audio forwarded.
        pipeline = Pipeline(
            [
                ParallelPipeline(
                    [
                        telephony_transport.input(),
                        _AudioForwarder("tel→daily"),
                        daily_transport.output(),
                    ],
                    [
                        daily_transport.input(),
                        _AudioForwarder("daily→tel"),
                        telephony_transport.output(),
                    ],
                )
            ]
        )

        task = PipelineTask(pipeline, enable_rtvi=False)
        runner = PipelineRunner(handle_sigint=False)

        # Register in the in-process registry so the status callback can reach us.
        _bridge_tasks[call_id] = task
        logger.info(f"[BridgeRun] Starting bridge for call {call_id}, room {room_url}")

        await runner.run(task)

    except Exception as e:
        _failed = True
        logger.error(
            f"[BridgeRun] Bridge exception for call {call_id}: {e}",
            exc_info=True,
        )
        await update_bridge_status(call_id, STATUS_FAILED, failure_reason=str(e))
        raise
    finally:
        _bridge_tasks.pop(call_id, None)

        # On a clean exit, mark the bridge disconnected so waiters know it's
        # over.  On a failure path the flag is already STATUS_FAILED (and the
        # monotonic guard in update_bridge_status will reject any overwrite), so
        # skip the STATUS_DISCONNECTED write to keep the failure signal intact.
        if not _failed:
            await update_bridge_status(call_id, STATUS_DISCONNECTED)

        # Release pool number if one was claimed by the warm-transfer handler.
        flag = await get_bridge_flag(call_id)
        if flag and flag.get("claimed") and flag.get("outbound_number_id"):
            oid = flag["outbound_number_id"]
            prov = (flag.get("provider") or "").lower()
            try:
                if prov in ("plivo", "exotel"):
                    await decrement_outbound_number_channels(oid)
                elif prov == "twilio":
                    await update_outbound_number_status(
                        oid, OutboundNumberStatus.AVAILABLE
                    )
                logger.info(
                    f"[BridgeRun] Released outbound number {oid} for call {call_id}"
                )
            except Exception as rel_err:
                logger.error(
                    f"[BridgeRun] Failed to release outbound number {oid}: {rel_err}"
                )

        await clear_bridge_flag(call_id)
        logger.info(f"[BridgeRun] Bridge ended for call {call_id}")


__all__ = ["run_bridge", "terminate_bridge"]
