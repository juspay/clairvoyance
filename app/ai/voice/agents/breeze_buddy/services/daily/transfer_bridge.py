"""
In-process audio bridge between a telephony WebSocket leg and a Daily room.

Used by the Daily warm-transfer flow: when the dialed agent picks up, the
telephony provider opens a WebSocket to the bridge endpoint. This module
builds a Pipecat dual-transport pipeline that forwards audio in both
directions with no STT/LLM/TTS in the path. Sample-rate conversion is
delegated to Pipecat's transport layer (input transport emits frames at its
native rate; output transport resamples to its own configured rate). The
telephony sender is unpaced (see _UnpacedFastAPIWebsocketOutputTransport) and
the daily→tel stream is primed with silence so event-loop jitter doesn't
become audible gaps on the phone leg.

V1 supports Plivo only. Exotel works through the same dispatcher path and
can be added by wiring its serializer + provider hangup; Twilio requires a
separate dial path (its `make_call` does not route through `/answer`).
"""

import asyncio
import os
from typing import Optional

import plivo
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
    FastAPIWebsocketOutputTransport,
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

# Daily bridge audio stays at 16 kHz on the WebRTC side. The Plivo side remains
# 8 kHz mu-law; Pipecat's output transport performs the 16k -> 8k conversion.
DAILY_BRIDGE_AUDIO_IN_SAMPLE_RATE = 16000
DAILY_BRIDGE_AUDIO_OUT_SAMPLE_RATE = 16000

# Plivo media frames are 20 ms at 8 kHz. Keep our outgoing playAudio cadence
# aligned with that instead of Pipecat's default 40 ms chunks.
TELEPHONY_BRIDGE_AUDIO_OUT_10MS_CHUNKS = 2

# Silence sent ahead of the first Daily audio frame so Plivo holds a standing
# playout buffer. The bridge shares the FastAPI event loop; combined with the
# unpaced sender below, this cushion keeps loop stalls up to this size from
# becoming audible gaps on the phone leg.
TELEPHONY_PRIME_MS = 160
BRIDGE_BOT_NAME = "transfer-bridge"

# ---------------------------------------------------------------------------
# In-process bridge task registry
# Maps bridge flag id → PipelineTask for active bridge pipelines.
# Used by the telephony status callback to terminate a bridge when the
# agent's call ends (rather than waiting for the WebSocket to close).
# ---------------------------------------------------------------------------
_bridge_tasks: dict[str, PipelineTask] = {}


async def terminate_bridge(bridge_id: str) -> bool:
    """Queue an EndFrame on the named bridge pipeline.

    Returns True if a running bridge was found and signalled, False otherwise.
    Safe to call from any async context in the same process.
    """
    task = _bridge_tasks.get(bridge_id)
    if not task:
        return False
    from pipecat.frames.frames import EndFrame

    await task.queue_frame(EndFrame())
    logger.info(f"[BridgeRun] Termination signalled for bridge {bridge_id}")
    return True


async def _hangup_agent_leg(call_id: str) -> None:
    """Best-effort hangup of the agent's PSTN leg via Plivo REST.

    Failure paths close the WebSocket, but with keepCallAlive=true Plivo keeps
    the call up — the agent would sit on a dead line until hanging up manually.
    Idempotent: Plivo returns 404 when the call already ended.
    """
    try:
        client = plivo.RestClient(PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN)
        await asyncio.to_thread(client.calls.delete, call_id)
        logger.info(f"[BridgeRun] Hung up agent leg {call_id} after bridge failure")
    except Exception as e:
        logger.warning(
            f"[BridgeRun] Agent-leg hangup for {call_id} skipped/failed: {e}"
        )


class _AudioForwarder(FrameProcessor):
    """Converts incoming InputAudioRawFrame to OutputAudioRawFrame so it can
    be sent out on a different transport's output side. All other frames
    (StartFrame, EndFrame, SystemFrame, etc.) are passed through unchanged so
    downstream output transports receive the lifecycle frames they need to
    initialise their MediaSender.

    When ``prime_ms`` is set, one silence frame of that duration is pushed
    ahead of the first audio frame. Downstream this becomes a standing playout
    buffer on the far end (Plivo queues playAudio and plays sequentially), so
    send-side jitter shorter than the prime is inaudible. The silence uses the
    incoming frame's sample rate so the output transport's stream resampler
    only ever sees one input rate.

    The optional label is used in periodic debug logs so audio flow can be
    confirmed from the logs without attaching a debugger.
    """

    def __init__(self, label: str = "", prime_ms: int = 0, tap: bool = False):
        super().__init__()
        self._label = label
        self._prime_ms = prime_ms
        self._primed = prime_ms <= 0
        self._count = 0
        # Debug tap: accumulate forwarded PCM in memory (~32 KB/s at 16 kHz)
        # so run_bridge can dump it to disk when the bridge ends.
        self._tap_buffer: Optional[bytearray] = bytearray() if tap else None
        self._tap_rate = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            if not self._primed:
                self._primed = True
                silence = b"\x00" * (
                    frame.sample_rate * 2 * frame.num_channels * self._prime_ms // 1000
                )
                await self.push_frame(
                    OutputAudioRawFrame(
                        audio=silence,
                        sample_rate=frame.sample_rate,
                        num_channels=frame.num_channels,
                    ),
                    direction,
                )
                logger.debug(
                    f"[BridgeRun] {self._label} primed {self._prime_ms}ms of silence"
                )
            if self._tap_buffer is not None:
                self._tap_buffer += frame.audio
                self._tap_rate = frame.sample_rate
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


class _UnpacedFastAPIWebsocketOutputTransport(FastAPIWebsocketOutputTransport):
    """Output transport without the per-chunk send throttle.

    Pipecat's ``_write_audio_sleep`` paces sends to real time and re-anchors
    its schedule whenever a chunk is late, so time lost to an event-loop stall
    is never recovered — each stall becomes a permanent gap in the playAudio
    stream (heard as crackling on the phone). The bridge's source is already
    real-time (the Daily room), so arrival order paces sends naturally; with
    the sleep removed, any backlog after a stall flushes immediately into
    Plivo's server-side buffer instead of gapping.
    """

    async def _write_audio_sleep(self):
        pass


class _UnpacedFastAPIWebsocketTransport(FastAPIWebsocketTransport):
    """FastAPIWebsocketTransport whose output side skips the send throttle."""

    def __init__(
        self,
        websocket: WebSocket,
        params: FastAPIWebsocketParams,
        input_name: Optional[str] = None,
        output_name: Optional[str] = None,
    ):
        super().__init__(websocket, params, input_name, output_name)
        self._output = _UnpacedFastAPIWebsocketOutputTransport(
            self, self._client, params, name=self._output_name
        )


def _build_telephony_serializer(provider: str, stream_id: str, call_id: str):
    """Pipecat serializer for the dialed agent leg."""
    if provider == "plivo":
        return PlivoFrameSerializer(
            stream_id=stream_id,
            call_id=call_id,
            auth_id=PLIVO_AUTH_ID,
            auth_token=PLIVO_AUTH_TOKEN,
            params=PlivoFrameSerializer.InputParams(sample_rate=TELEPHONY_SAMPLE_RATE),
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
    flag_id: str,
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
        call_id: provider call sid.
        flag_id: Redis bridge flag key — the generated pre-dial bridge id,
            carried on the answer/status/WS URLs as ``bridge_id``. Also the
            in-process task-registry key.
        room_url: Daily room URL the AI bot is in.
        daily_token: fresh owner token minted by the Daily handler.
    """
    _failed = False  # tracks whether an exception path already set STATUS_FAILED

    # Debug tap: set BRIDGE_AUDIO_TAP_DIR to capture both directions as raw
    # PCM (s16le mono) for offline echo/quality analysis. Deliberately a plain
    # env read, not part of the static config surface — diagnostic only.
    tap_dir = os.getenv("BRIDGE_AUDIO_TAP_DIR", "")
    forward_tel_to_daily = _AudioForwarder("tel→daily", tap=bool(tap_dir))
    forward_daily_to_tel = _AudioForwarder(
        "daily→tel", prime_ms=TELEPHONY_PRIME_MS, tap=bool(tap_dir)
    )

    try:
        # Build transports and pipeline inside the try block so that errors
        # here (e.g. NotImplementedError for unsupported providers) are caught,
        # published as STATUS_FAILED, and re-raised rather than leaving the
        # warm-transfer poller spinning until its timeout fires.
        serializer = _build_telephony_serializer(provider, stream_id, call_id)

        telephony_transport = _UnpacedFastAPIWebsocketTransport(
            websocket=websocket,
            params=FastAPIWebsocketParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,
                audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
                audio_out_10ms_chunks=TELEPHONY_BRIDGE_AUDIO_OUT_10MS_CHUNKS,
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
                # Read Daily as the mixed room speaker stream. Capturing each
                # participant track separately and appending frames into one
                # PSTN output stream can sound like jitter/stutter.
                audio_in_user_tracks=False,
                audio_in_sample_rate=DAILY_BRIDGE_AUDIO_IN_SAMPLE_RATE,
                audio_out_sample_rate=DAILY_BRIDGE_AUDIO_OUT_SAMPLE_RATE,
            ),
        )

        @daily_transport.event_handler("on_joined")
        async def on_joined(transport, data):
            await update_bridge_status(flag_id, STATUS_JOINED)
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
                        forward_tel_to_daily,
                        daily_transport.output(),
                    ],
                    [
                        daily_transport.input(),
                        forward_daily_to_tel,
                        telephony_transport.output(),
                    ],
                )
            ]
        )

        # cancel_on_idle_timeout must be off: idle detection resets only on
        # BotSpeakingFrame/UserSpeakingFrame, and this pipeline (no TTS, no
        # VAD) emits neither — the default would kill every bridge at 300s.
        task = PipelineTask(pipeline, enable_rtvi=False, cancel_on_idle_timeout=False)
        runner = PipelineRunner(handle_sigint=False)

        # Daily's virtual speaker selection (used for the mixed room stream) is
        # process-global: a second concurrent bridge in this pod steals the
        # room mix from the first, cross-feeding audio between the calls —
        # each agent hears the other bridge's track (echo loops, and a privacy
        # leak across unrelated calls). Refuse instead of corrupting both;
        # this transfer fails cleanly and the AI conversation continues.
        if _bridge_tasks:
            raise RuntimeError("concurrent_bridge_unsupported_in_this_process")

        # Register in the in-process registry so the status callback can reach us.
        _bridge_tasks[flag_id] = task
        logger.info(f"[BridgeRun] Starting bridge for call {call_id}, room {room_url}")

        await runner.run(task)

    except Exception as e:
        _failed = True
        logger.error(
            f"[BridgeRun] Bridge exception for call {call_id}: {e}",
            exc_info=True,
        )
        await update_bridge_status(flag_id, STATUS_FAILED, failure_reason=str(e))
        await _hangup_agent_leg(call_id)
        raise
    finally:
        if tap_dir:
            for fwd, tag in (
                (forward_tel_to_daily, "tel_to_daily"),
                (forward_daily_to_tel, "daily_to_tel"),
            ):
                if not fwd._tap_buffer:
                    continue
                path = os.path.join(
                    tap_dir, f"bridge-{call_id}-{tag}-{fwd._tap_rate}hz.raw"
                )
                try:
                    with open(path, "wb") as f:
                        f.write(fwd._tap_buffer)
                    logger.info(
                        f"[BridgeRun] Audio tap written: {path} (play with: "
                        f"ffplay -f s16le -ar {fwd._tap_rate} -ch_layout mono "
                        f"'{path}')"
                    )
                except Exception as tap_err:
                    logger.warning(
                        f"[BridgeRun] Audio tap write failed for {path}: {tap_err}"
                    )

        _bridge_tasks.pop(flag_id, None)

        # On a clean exit, mark the bridge disconnected so waiters know it's
        # over.  On a failure path the flag is already STATUS_FAILED (and the
        # monotonic guard in update_bridge_status will reject any overwrite), so
        # skip the STATUS_DISCONNECTED write to keep the failure signal intact.
        if not _failed:
            await update_bridge_status(flag_id, STATUS_DISCONNECTED)

        # Release pool number if one was claimed by the warm-transfer handler.
        flag = await get_bridge_flag(flag_id)
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

        await clear_bridge_flag(flag_id)
        logger.info(f"[BridgeRun] Bridge ended for call {call_id}")


__all__ = ["run_bridge", "terminate_bridge"]
