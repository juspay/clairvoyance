"""Transport configuration for voice agents.

Note: VAD (Voice Activity Detection) is configured in the LLMUserAggregator
(via UserTurnStrategies), NOT in the transport. The transport only handles
audio I/O, sample rates, and optional audio filters/mixers.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import WebSocket
from pipecat.audio.filters.aic_filter import AICFilter
from pipecat.audio.filters.base_audio_filter import BaseAudioFilter
from pipecat.runner.utils import _create_telephony_transport
from pipecat.transports.base_transport import BaseTransport
from pipecat.transports.daily.transport import DailyParams
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from app.ai.voice.agents.breeze_buddy.services.telephony.vobiz.serializer import (
    VobizFrameSerializer,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    NoiseFilterModel,
    NoiseFilterProvider,
    NoiseFilterType,
    TemplateModel,
)
from app.ai.voice.agents.breeze_buddy.template.vad import TELEPHONY_SAMPLE_RATE
from app.ai.voice.agents.breeze_buddy.utils.audio_mixer import (
    create_background_sound_mixer,
)
from app.ai.voice.noise_filter.noizzen import NoizzenFilter, find_model_file
from app.core.config import static
from app.core.logger import logger

# Constants
TRANSPORT_TYPE_DAILY = "daily"
TRANSPORT_TYPE_TELEPHONY = "telephony"
TRANSPORT_TYPE_VOBIZ = "vobiz"


# Template model -> Noizzen model (False = telephony 8 kHz, True = Daily 16 kHz).
# No primary-speaker model exists, so voice_focus maps to the noise canceller.
_NOISE_MODELS = {
    False: {
        NoiseFilterModel.NOISE_CANCELLATION: "dpdfnet2_8k",
        NoiseFilterModel.NOISE_CANCELLATION_S: "dpdfnet2_8k",
        NoiseFilterModel.VOICE_FOCUS: "dpdfnet2_8k",
    },
    True: {
        NoiseFilterModel.NOISE_CANCELLATION: "dpdfnet_base_16k",
        NoiseFilterModel.NOISE_CANCELLATION_S: "dpdfnet_base_16k",
        NoiseFilterModel.VOICE_FOCUS: "dpdfnet_base_16k",
    },
}
_missing_models_logged: set = set()


def _create_noizzen_filter(
    model: NoiseFilterModel,
    transport_type: str,
    enhancement_level: Optional[float],
) -> Optional[BaseAudioFilter]:
    """The Noizzen filter for a template model, or None when its file is missing."""
    name = _NOISE_MODELS[transport_type == TRANSPORT_TYPE_DAILY][model]
    path = find_model_file(name, Path(static.BREEZE_BUDDY_NOISE_MODEL_DIR))
    if path is None:
        if name not in _missing_models_logged:  # once per process, not per call
            _missing_models_logged.add(name)
            logger.warning(
                "Noise model {} is not provisioned in {}; proceeding without a "
                "noise filter",
                name,
                static.BREEZE_BUDDY_NOISE_MODEL_DIR,
            )
        return None
    try:
        noise_filter = NoizzenFilter(
            model=name, model_path=path, enhancement_level=enhancement_level
        )
    except Exception as e:
        logger.warning(f"Failed to create noise filter, proceeding without it: {e}")
        return None
    logger.info(
        "Noise filter initialized: provider=noizzen, model={} ({}), "
        "enhancement_level={}",
        model.value,
        name,
        enhancement_level,
    )
    return noise_filter


# --- ai-coustics (AIC): templates with provider "aic" or a legacy `type`. ---


def _aic_model_path(model: NoiseFilterModel, transport_type: str) -> Path:
    """The artifact for a model on a transport.

    The model value carries the size (noise_cancellation = quail-L,
    noise_cancellation_s = quail-S); the transport picks the processing rate:
    Daily (web) uses the 16kHz artifact, telephony (Twilio/Plivo/Exotel) the 8kHz
    one. These rates describe the model, not the transport's native rate: Daily
    may run at 24kHz elsewhere in the pipeline while still using the 16kHz model
    here. Voice Focus ships only a 16kHz artifact; the SDK resamples 8kHz input.
    """
    web = transport_type == TRANSPORT_TYPE_DAILY
    paths = {
        NoiseFilterModel.NOISE_CANCELLATION: (
            static.AIC_MODEL_PATH_16KHZ if web else static.AIC_MODEL_PATH
        ),
        NoiseFilterModel.NOISE_CANCELLATION_S: (
            static.AIC_MODEL_PATH_S_16KHZ if web else static.AIC_MODEL_PATH_S
        ),
        NoiseFilterModel.VOICE_FOCUS: static.AIC_VOICE_FOCUS_MODEL_PATH,
    }
    return Path(paths[model])


def _resolve_aic_model(
    model: NoiseFilterModel, transport_type: str
) -> tuple[NoiseFilterModel, Path]:
    """The model to load and its artifact.

    A missing quail-S file falls back to quail-L (noise_cancellation), the model
    every template ran before quail-S existed, so a soft-failed Docker copy
    degrades to the known model instead of dropping the filter. The returned
    model is the one loaded, so logs never report quail-S for a quail-L file.
    """
    path = _aic_model_path(model, transport_type)
    if model == NoiseFilterModel.NOISE_CANCELLATION_S and not path.is_file():
        logger.warning(
            "AIC quail-S model file is unavailable; using noise_cancellation "
            "(quail-L) instead: {}",
            path,
        )
        model = NoiseFilterModel.NOISE_CANCELLATION
        path = _aic_model_path(model, transport_type)
    return model, path


# AIC processor teardown blocks on its usage report; keep it off the event loop.
_AIC_TEARDOWN = ThreadPoolExecutor(max_workers=4, thread_name_prefix="aic-teardown")


class _FailOpenAICFilter(AICFilter):
    """AICFilter that never deafens a call or stalls the event loop.

    pipecat's AICFilter lets SDK errors escape (e.g. a refused license key) and
    base_input only catches timeouts, so the bot would stop hearing the caller.
    Any AIC failure hands the rest of the call to `fallback` (the Noizzen filter),
    or to passthrough. Native handles are freed on _AIC_TEARDOWN, not the loop.
    """

    def __init__(
        self, *, fallback: Callable[[], Optional[BaseAudioFilter]], **kwargs: Any
    ):
        super().__init__(**kwargs)
        self._make_fallback = fallback
        self._fallback: Optional[BaseAudioFilter] = None
        self._fallback_tried = False
        self._rate = 0

    async def _switch_to_fallback(self, reason: str) -> None:
        if self._fallback_tried:
            return
        self._fallback_tried = True
        self._aic_ready = False  # AICFilter.filter passes audio through when not ready
        fallback = self._make_fallback()
        if fallback is not None and self._rate:
            await fallback.start(self._rate)
            self._fallback = fallback
        logger.warning(
            "AIC filter unavailable ({}); {} for the rest of the call",
            reason,
            (
                "using the Noizzen noise filter"
                if self._fallback
                else "passing audio through"
            ),
        )

    async def start(self, sample_rate: int):
        self._rate = sample_rate
        try:
            await super().start(sample_rate)
        except Exception as e:  # e.g. a corrupt .aicmodel
            await self._switch_to_fallback(f"start failed: {e}")
            return
        if not self._aic_ready:  # pipecat already logged why (e.g. key refused)
            await self._switch_to_fallback("processor not created")

    async def filter(self, audio: bytes) -> bytes:
        if self._fallback is not None:
            return await self._fallback.filter(audio)
        # AICFilter.filter takes buffered audio out before processing it; keep
        # the raw bytes so a failure hands them back instead of dropping them.
        pending = bytes(self._audio_buffer) + audio
        try:
            return await super().filter(audio)
        except Exception as e:
            self._audio_buffer.clear()
            await self._switch_to_fallback(f"failed mid-call: {e}")
            return pending

    async def stop(self):
        handles = [self._processor, self._processor_ctx, self._vad_ctx]
        try:
            await super().stop()
        except Exception as e:  # the processor reset; AICFilter.stop still cleans up
            logger.warning("AIC filter cleanup failed at call end: {}", e)
        finally:
            if self._fallback is not None:
                await self._fallback.stop()
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(_AIC_TEARDOWN, handles.clear)


def _create_aic_filter(
    model: NoiseFilterModel,
    transport_type: str,
    enhancement_level: Optional[float],
) -> Optional[BaseAudioFilter]:
    """The AIC filter, or the Noizzen filter whenever AIC can't run."""

    # AIC's enhancement_level is on a different scale: the fallback uses Noizzen's own.
    def noizzen() -> Optional[BaseAudioFilter]:
        return _create_noizzen_filter(model, transport_type, None)

    if not static.BREEZE_BUDDY_AIC_LICENSE_KEY:
        logger.warning(
            "Template asks for AIC but no license key is configured; using Noizzen"
        )
        return noizzen()
    aic_model, model_path = _resolve_aic_model(model, transport_type)
    if not model_path.is_file():
        logger.warning(
            "AIC filter model file is unavailable; using Noizzen: {}",
            model_path,
        )
        return noizzen()
    # No level given: let the AIC model use its own default.
    level = (
        {} if enhancement_level is None else {"enhancement_level": enhancement_level}
    )
    try:
        aic_filter = _FailOpenAICFilter(
            fallback=noizzen,
            license_key=static.BREEZE_BUDDY_AIC_LICENSE_KEY,
            model_path=model_path,
            **level,
        )
    except Exception as e:
        logger.warning(f"Failed to initialize AIC filter, using Noizzen: {e}")
        return noizzen()
    logger.info(
        "AIC filter initialized: provider=aic, model={}, path={}, enhancement_level={}",
        aic_model.value,
        model_path,
        enhancement_level,
    )
    return aic_filter


def _create_audio_input_filter(
    configurations: Optional[ConfigurationModel] = None,
    transport_type: str = TRANSPORT_TYPE_DAILY,
) -> Optional[BaseAudioFilter]:
    """The noise filter for a template's noise_filter config, or None.

    The template alone decides: provider "noizzen" gets Noizzen; provider "aic"
    and the legacy `type` get ai-coustics, and a call falls back to Noizzen when
    ai-coustics cannot run. A missing model file means no filter, never a failed
    call.
    """
    if not configurations:
        return None
    config = configurations.noise_filter
    if not config or not config.enable:
        return None

    model = config.model or (
        NoiseFilterModel.VOICE_FOCUS
        if config.type == NoiseFilterType.AIC_VOICE_FOCUS
        else NoiseFilterModel.NOISE_CANCELLATION
    )
    if config.provider != NoiseFilterProvider.NOIZZEN:
        return _create_aic_filter(model, transport_type, config.enhancement_level)
    return _create_noizzen_filter(model, transport_type, config.enhancement_level)


def get_transport_params(
    template: Optional[TemplateModel] = None,
    configurations: Optional[ConfigurationModel] = None,
    daily_audio_out_10ms_chunks: int = 10,
) -> dict:
    """Get transport parameters dictionary for all transport types.

    VAD is not configured here — it lives in the LLMUserAggregator
    (via vad_analyzer + UserTurnStrategies) where it feeds turn detection.

    Args:
        template: Optional template model for background sound mixer configuration
        configurations: Optional configuration model for settings (e.g., noise filter)
        daily_audio_out_10ms_chunks: Daily output write size in 10ms chunks.
            The live default comes from the BB_DAILY_AUDIO_OUT_10MS_CHUNKS
            dynamic config, resolved by the async caller and passed in (this
            function is sync); only the daily transport uses it, so telephony
            callers leave the fallback default.

    Returns:
        Dictionary mapping transport types to parameter factory functions
    """
    # Create separate mixers per transport so each uses the correct sample rate.
    # Daily runs at 24 kHz; all telephony transports run at TELEPHONY_SAMPLE_RATE (8 kHz).
    daily_mixer = create_background_sound_mixer(template, sample_rate=24000)
    telephony_mixer = create_background_sound_mixer(
        template, sample_rate=TELEPHONY_SAMPLE_RATE
    )

    return {
        "daily": lambda: DailyParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            # Output write size in 10ms chunks (pipecat default 4 = 40ms):
            # bigger chunks give the paced audio writer more event-loop slack
            # against stalls in the bot process, which otherwise underrun
            # Daily's virtual mic and the browser hears crackling
            # (pipecat#331). Rationale + trade-offs on the
            # BB_DAILY_AUDIO_OUT_10MS_CHUNKS dynamic config.
            audio_out_10ms_chunks=daily_audio_out_10ms_chunks,
            audio_out_mixer=daily_mixer,
            audio_in_filter=_create_audio_input_filter(
                configurations, TRANSPORT_TYPE_DAILY
            ),
        ),
        "twilio": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_mixer=telephony_mixer,
            audio_in_filter=_create_audio_input_filter(
                configurations, TRANSPORT_TYPE_TELEPHONY
            ),
        ),
        "exotel": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_mixer=telephony_mixer,
            audio_in_filter=_create_audio_input_filter(
                configurations, TRANSPORT_TYPE_TELEPHONY
            ),
        ),
        "telnyx": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_mixer=telephony_mixer,
            audio_in_filter=_create_audio_input_filter(
                configurations, TRANSPORT_TYPE_TELEPHONY
            ),
        ),
        "plivo": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_mixer=telephony_mixer,
            audio_in_filter=_create_audio_input_filter(
                configurations, TRANSPORT_TYPE_TELEPHONY
            ),
        ),
        "vobiz": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_mixer=telephony_mixer,
            audio_in_filter=_create_audio_input_filter(
                configurations, TRANSPORT_TYPE_TELEPHONY
            ),
        ),
    }


async def create_telephony_transport(
    websocket: WebSocket,
    params: Any,
    transport_type: str,
    call_data: dict,
) -> BaseTransport:
    """Build the telephony transport for an already-parsed call.

    pipecat's factory knows twilio/telnyx/plivo/exotel. A Vobiz stream opens
    with a Plivo-shaped start event, so pipecat detects it as "plivo" and
    would hang up through api.plivo.com with PLIVO_* keys. The agent relabels
    those calls "vobiz" from the websocket path, and they get
    VobizFrameSerializer here instead. Used by the first build and by the
    agent-to-agent rebuild, so both stay on the same serializer.
    """
    if transport_type != TRANSPORT_TYPE_VOBIZ:
        return await _create_telephony_transport(
            websocket, params, transport_type, call_data
        )

    params.add_wav_header = False
    params.serializer = VobizFrameSerializer(
        stream_id=call_data["stream_id"],
        call_id=call_data["call_id"],
        auth_id=static.VOBIZ_AUTH_ID,
        auth_token=static.VOBIZ_AUTH_TOKEN,
    )
    return FastAPIWebsocketTransport(websocket=websocket, params=params)
