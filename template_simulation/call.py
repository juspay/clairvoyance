"""One simulated call, through the real production pipeline.

``build_pipeline`` is the exact call a live telephony session makes — real
STT, TTS, VAD and flow engine. Only the transport is replaced, reading and
writing buffers instead of a carrier socket, so mishearing and turn-taking
bugs surface here the way they would on a real line. Everything else in this
file exists to keep the call side-effect free: a bot façade, capture-only
hooks, and mocks for the boundary-crossing handlers.
"""

from __future__ import annotations

import asyncio
import audioop
import copy
import io
import re
import shutil
import tempfile
import time
import traceback
import uuid
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    EndFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    InputAudioRawFrame,
    InterruptionFrame,
    LLMMessagesAppendFrame,
    OutputAudioRawFrame,
    StartFrame,
    TranscriptionFrame,
    TTSTextFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

from app.ai.voice.agents.breeze_buddy.handlers.internal.builtin_dispatcher import (
    builtin_function_dispatcher as real_builtin_dispatcher,
)
from app.ai.voice.agents.breeze_buddy.template.builder import FlowConfigBuilder
from app.ai.voice.agents.breeze_buddy.template.context import (
    TemplateContext,
    with_context,
)
from app.ai.voice.agents.breeze_buddy.template.hooks import Hook, HookRegistry
from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    FieldSource,
    HookConfig,
    TemplateModel,
    TTSProvider,
)
from app.ai.voice.agents.breeze_buddy.template.utils import render_messages_with_vars
from app.ai.voice.agents.breeze_buddy.template.vad import TELEPHONY_SAMPLE_RATE
from app.ai.voice.llm.realtime.gemini.realtime import has_realtime_llm
from app.core.logger import logger
from app.schemas.breeze_buddy.core import LeadCallTracker
from app.utils.transformation import TEMPLATE_FUNCTION_REGISTRY
from template_simulation import config as cfg
from template_simulation.suite import (
    EndReason,
    HookRecord,
    SimCase,
    SimTrace,
    Speaker,
    ToolCallRecord,
    Turn,
)

# Telephony carries 8 kHz mono. Matching it matters: STT accuracy is
# sample-rate sensitive, and testing at 24 kHz flatters the agent with audio
# cleaner than any real call it will ever take.
SAMPLE_RATE = TELEPHONY_SAMPLE_RATE
_BYTES_PER_SAMPLE = 2
_FRAME_MS = 20
_FRAME_BYTES = int(SAMPLE_RATE * _FRAME_MS / 1000.0) * _BYTES_PER_SAMPLE

# Safety valve on waiting for the agent's BotStoppedSpeakingFrame — not a
# turn-taking knob (that is event-driven), just a ceiling for a turn that
# never signals at all.
_AGENT_TURN_TIMEOUT_S = 45.0
# Long enough that the agent has genuinely started speaking, short enough to
# be a real mid-sentence interruption.
_BARGE_IN_DELAY_S = 1.5
# Silence after each customer utterance, so VAD sees the turn end. Not
# padding — it is the signal.
_TRAILING_SILENCE_S = 1.2
# A male ElevenLabs voice for the customer, distinct from the agent's.
_PERSONA_VOICE_ID = "SLa3GDQHUGaRpH5GNvFL"

_PERSONA_RULES = """\
You are role-playing a CUSTOMER on a phone call. You are NOT an assistant.

- Stay in character; never mention being an AI or a simulation.
- Speak like a person on the phone: one or two short sentences, plain,
  sometimes hesitant. No lists, no markdown, no stage directions.
- Do not be artificially helpful. Answer what you are asked, nothing more.
- You are the one being CALLED. You never speak for the business: you do not
  apologise on its behalf, offer it options, explain its policy, ask the
  caller what they would like to do, or say the company's lines back to it.
  If your reply could have come from the agent, you have slipped character —
  answer as the person on the receiving end instead.
- You came onto this call wanting something specific — it is in your character
  below. RAISE IT YOURSELF, in your own words, early. Do not sit and answer
  whatever the agent happens to ask and let the call end without it: an agent
  who never hears what you want cannot be judged on how it handled you. If the
  agent steers elsewhere, answer briefly and come back to what you wanted.
- Play your character THROUGH to the end. If they are described as changing
  their mind, running out of time or getting annoyed, actually reach that
  point — do not end before it has happened. Never say you are busy, in a
  hurry or want a callback unless your character actually says so; those end
  the call immediately and your scenario never happens.
- Everything you write is SPOKEN ALOUD, so write only what a mouth can say:
  numbers and amounts as words, never digits or symbols — "aṭhārah sau
  ninyānave rupaye" / "eighteen ninety-nine rupees", never "₹1899". A written
  amount is read out as a glyph plus digits, which splits your sentence in two
  mid-way and comes back mis-heard; the agent then answers half a request
  twice.
- Reply with exactly <END> only once there is nothing left to say.
- If your character pauses, hesitates or does not respond this turn — goes
  quiet, does not answer, needs a moment — reply with EXACTLY <SILENT> and
  nothing else: that literal tag, in English letters, with no brackets
  swapped, no quotes around it, no translation or transliteration into the
  language you are speaking, and no other character in the reply. Every word
  you write outside that exact tag is SPOKEN ALOUD and HEARD, so "Silence.",
  "[quiet]", or the word for silent in any language is not silence — it is
  you saying that word out loud.
- Speak the language the agent is speaking, and keep to it.
- You are voiced by a MALE speaker: in any gendered language use masculine
  forms throughout ("kar raha hoon", never "kar rahi hoon"), whatever the
  character's name turns out to be.
"""

# A customer cannot be finished before they have said anything — models
# sometimes emit <END> on the opening turn and truncate the whole case.
_MIN_TURNS_BEFORE_END = 2

# What next() returns for "stayed silent" — never spoken text, so it can never
# collide with a real reply. A "no response" case has no other way to say
# nothing: an empty string either gets overwritten with a filler question
# (early turns) or is read as PERSONA_DONE (later), and either way the model,
# with no real way to represent silence, wrote garbled words performing it.
_SILENCE = "\x00sim-silence\x00"


# ---------------------------------------------------------------------------
# The fake bot: everything TemplateContext reads, nothing that dials out
# ---------------------------------------------------------------------------

SIM_HOOK_PREFIX = "sim_capture__"
# Voice-only side effects: keep the name visible to the builder, do nothing.
NOOP_HANDLERS = ("mute_stt", "unmute_stt", "play_audio_sound", "send_alert")
TERMINAL_HANDLERS = {
    "end_conversation": "ended",
    "connect_to_live_agent": "transferred",
}
# builtin_function_dispatcher's own registry (handlers/internal/
# builtin_dispatcher.py:BUILTIN_HANDLERS), split the same way the top-level
# handlers above are: ends the call, or is a real side effect that must not
# run for real. get_current_time is neither and is left to run for real —
# read-only, no side effect, and a template may reason about its result.
_BUILTIN_TERMINAL = {
    "connect_to_agent": "transferred",
    "connect_to_live_agent": "transferred",
    "hold_and_consult": "transferred",
    "end_conversation": "ended",
}
_BUILTIN_NOOP = ("update_outcome", "query_knowledge_base")


class SimTerminated(Exception):
    """Raised by the mocked terminal handlers, so the handler chain stops at
    the point production would have torn the call down."""

    def __init__(self, reason: str, node: Optional[str] = None) -> None:
        super().__init__(f"conversation terminated: {reason}")
        self.reason = reason
        self.node = node


class SimBot:
    """The bot façade. The synthetic lead is the trick that matters:
    ``record_node_entry/exit`` early-return when ``lead`` is None (which is
    why chat mode has no traversal data), so giving it a real in-memory lead
    makes the REAL engine emit the full node path for free."""

    def __init__(
        self, *, case_id: str, template: TemplateModel, payload: Dict[str, Any]
    ) -> None:
        self.template = template
        self.case_id = case_id
        # Namespaces this run's hook registrations — HookRegistry._hooks is
        # class-level, so concurrent cases would otherwise overwrite each
        # other's capture binding and record into one arbitrary bot.
        self.run_token = uuid.uuid4().hex[:12]

        self.flow_config: Optional[Dict[str, Any]] = None
        self.vad_analyzer = None
        self.speech_gate = None
        self.lead = LeadCallTracker(
            id=f"sim-{case_id}",
            reseller_id=template.reseller_id or "sim-reseller",
            merchant_id=template.merchant_id,
            template=template.name,
            template_id=template.id,
            payload=dict(payload),
            metaData={},
        )
        self.call_sid = f"sim-{case_id}"
        # None deliberately: http handlers are mocked, so anything reaching
        # for a real session is an un-mocked network path and must fail loud.
        self.aiohttp_session = None
        self.transport = None
        self.task: Optional[PipelineTask] = None
        self.telephony_service = None
        self.provider = None
        self.completion_function = None
        self.context = None
        self.root_span = None
        self.configurations: Optional[ConfigurationModel] = template.configurations
        self.end_conversation_callbacks: List[Any] = []
        self.expected_callback_response_schema = (
            template.expected_callback_response_schema
        )
        self.conversation_ended = False
        self.end_reason: Optional[str] = None
        self.end_node: Optional[str] = None
        # Chat sets both so the shared global-function wrapper does not
        # double-apply approval gating / state reducers. A simulation wants
        # the same.
        self.handles_approval_externally = True
        self.handles_state_externally = True

        self.recorded_hooks: List[HookRecord] = []
        self.recorded_actions: List[str] = []

    async def queue_tts_filler(self, phrase: str) -> None:
        self.recorded_actions.append(f"tts_filler:{phrase}")

    async def manage_audio_mixer(self, *args: Any, **kwargs: Any) -> None:
        self.recorded_actions.append("manage_audio_mixer")

    @property
    def node_traversal(self) -> List[Dict[str, Any]]:
        return (self.lead.metaData or {}).get("node_traversal", [])


class _CaptureHook(Hook):
    """Runs the real field-resolution logic, then records instead of writing —
    mirrors UpdateOutcomeInDatabaseHook.execute, so what is asserted on is
    what production would have persisted."""

    def __init__(self, name: str, bot: SimBot) -> None:
        super().__init__(name)
        self._bot = bot

    async def execute(
        self,
        context: TemplateContext,
        args: Dict[str, Any],
        function_name: str,
        hook_config: HookConfig,
    ) -> None:
        resolved: Dict[str, Any] = {}
        for name, spec in (hook_config.expected_fields or {}).items():
            if spec.source == FieldSource.STATIC:
                resolved[name] = spec.value
            elif spec.source == FieldSource.LLM:
                # The template names the ARG to read; not always the field name.
                value = args.get(spec.value or name, args.get(name))
                if value is not None:
                    resolved[name] = value
            elif spec.source == FieldSource.COMPUTED and spec.value:
                try:
                    from app.ai.voice.agents.breeze_buddy.handlers.transport.utils.computed_fields import (  # noqa: E501
                        resolve_computed_value,
                    )

                    resolved[name] = resolve_computed_value(spec.value)
                except Exception as e:  # best-effort
                    logger.debug(f"[sim] computed field {name} failed: {e}")
        self._bot.recorded_hooks.append(
            HookRecord(hook_config.name, function_name, resolved)
        )


class MockHttpTool:
    """Stands in for http_function_handler and MCP tool handlers.

    The envelope shape matters as much as the payload: templates branch on it
    (a WhatsApp send is only confirmed on status success AND a 200-range
    status_code), so a bare {"status": "success"} makes a correct agent's
    retry look like spamming."""

    def __init__(self) -> None:
        self.calls: List[Any] = []

    def response_for(self, name: str, args: Dict[str, Any]) -> Any:
        self.calls.append((name, dict(args)))
        return {"status": "success", "status_code": 200, "data": {"simulated": True}}


def install_handler_mocks(builder: Any, bot: SimBot, http_mock: MockHttpTool) -> None:
    """Swap side-effecting handlers on OUR builder instance only (handler_map
    is per-instance), in the same order chat mode does it."""

    def _noop(handler_name: str):
        async def run(context: Any, args: Dict[str, Any], *a: Any, **kw: Any):
            bot.recorded_actions.append(handler_name)
            return {"status": "success"}, None

        return run

    def _terminal(handler_name: str, reason: str):
        async def run(context: Any, args: Dict[str, Any], *a: Any, **kw: Any):
            bot.recorded_actions.append(handler_name)
            traversal = bot.node_traversal
            # Set BEFORE raising: the exception is swallowed deep inside
            # pipecat's function-call executor and never reaches the runner.
            bot.conversation_ended = True
            bot.end_reason = reason
            bot.end_node = traversal[-1].get("node_name") if traversal else None
            # Production queues a real EndFrame (handlers/internal/
            # end_conversation.py); the pipeline physically stops on it.
            # Anything less keeps STT/LLM/TTS running after the call.
            if bot.task is not None:
                await bot.task.queue_frame(EndFrame())
            raise SimTerminated(reason, bot.end_node)

        return run

    async def _http(context: Any, args: Dict[str, Any], *a: Any, **kw: Any):
        function_config = kw.get("function_config") or (a[0] if a else None)
        name = getattr(function_config, "name", "unknown_http_function")
        return http_mock.response_for(name, args), None

    async def _builtin(context: Any, args: Dict[str, Any], *a: Any, **kw: Any):
        function_config = kw.get("function_config") or (a[0] if a else None)
        handler_name = str(getattr(function_config, "handler", "") or "")
        if handler_name in _BUILTIN_TERMINAL:
            return await _terminal(handler_name, _BUILTIN_TERMINAL[handler_name])(
                context, args, *a, **kw
            )
        if handler_name in _BUILTIN_NOOP:
            return await _noop(handler_name)(context, args, *a, **kw)
        return await real_builtin_dispatcher(context, args, *a, **kw)

    for name in NOOP_HANDLERS:
        if name in builder.handler_map:
            builder.handler_map[name] = _noop(name)
    for name, reason in TERMINAL_HANDLERS.items():
        if name in builder.handler_map:
            builder.handler_map[name] = _terminal(name, reason)
    if "http_function_handler" in builder.handler_map:
        builder.handler_map["http_function_handler"] = _http
    if "builtin_function_dispatcher" in builder.handler_map:
        builder.handler_map["builtin_function_dispatcher"] = _builtin
    if "custom_python_code_handler" in builder.handler_map:
        builder.handler_map["custom_python_code_handler"] = _noop(
            "custom_python_code_handler"
        )


def neutralize_hooks(template: TemplateModel, bot: SimBot) -> TemplateModel:
    """A copy whose hooks point at additive, capture-only registrations. The
    original template and every real registry entry are untouched."""
    clone = template.model_copy(deep=True)
    flow = copy.deepcopy(clone.flow or {})
    renamed: set = set()

    def rewrite(container: Dict[str, Any]) -> None:
        for fn in container.get("functions") or []:
            if not isinstance(fn, dict):
                continue
            for hook in fn.get("hooks") or []:
                if not isinstance(hook, dict):
                    continue
                original = hook.get("name")
                if not original or original.startswith(SIM_HOOK_PREFIX):
                    continue
                hook["name"] = f"{SIM_HOOK_PREFIX}{bot.run_token}__{original}"
                # transition_handler otherwise fires hooks via create_task, and
                # a terminal transition can end the run before that task runs.
                hook["awaited"] = True
                renamed.add(hook["name"])

    for node in flow.get("nodes") or []:
        rewrite(node)
    rewrite(flow)  # direct mode keeps functions at the flow root

    for name in renamed:
        HookRegistry.register(name, _CaptureHook(name, bot))
    clone.flow = flow
    return clone


def restore_hook_name(sim_name: str) -> str:
    if not sim_name.startswith(SIM_HOOK_PREFIX):
        return sim_name
    _, sep, original = sim_name[len(SIM_HOOK_PREFIX) :].partition("__")
    return original if sep else sim_name


def release_sim_hooks(bot: SimBot) -> None:
    """Drop only this run's registrations — never a real entry."""
    prefix = f"{SIM_HOOK_PREFIX}{bot.run_token}__"
    for name in [n for n in HookRegistry.get_all() if n.startswith(prefix)]:
        HookRegistry._hooks.pop(name, None)


def _catalog_response(tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """A plausible catalog response, shaped like production's tool_response
    transforms. The default {"simulated": true} has no products array, so
    search_catalog looks empty and a correct agent saying "I don't have that"
    reads as an agent failure when it is a harness one."""
    catalog = args.get("catalog") if isinstance(args.get("catalog"), dict) else {}
    label = (
        str(catalog.get("query") or catalog.get("id") or "").strip()
        or "Wellness Product"
    )
    product = {
        "id": f"gid://shopify/Product/sim-{abs(hash(label)) % 100000}",
        "title": label.title(),
        "description": f"A {label} sold by this store.",
        "url": f"https://example.test/products/{label.lower().replace(' ', '-')}",
        "variants": [
            {"title": "Default Title", "price": "499.00", "availability": "in_stock"}
        ],
    }
    return (
        {"product": product} if tool_name == "get_product" else {"products": [product]}
    )


def build_mcp_mocks(
    template: TemplateModel, bot: SimBot, http_mock: MockHttpTool
) -> List[Any]:
    """Offline stand-ins for the template's MCP tools, built from the inline
    manifest so the LLM sees definitions identical to production without a
    single round-trip. A server with no inline schemas is flagged, not
    silently dropped."""
    from pipecat_flows import FlowsFunctionSchema

    cfg_model = template.configurations
    mcp = getattr(cfg_model, "mcp", None) if cfg_model else None
    out: List[Any] = []
    seen: set = set()
    for server in getattr(mcp, "servers", None) or []:
        if not getattr(server, "enabled", False):
            continue
        schemas = getattr(server, "tool_schemas", None) or []
        if not schemas:
            bot.recorded_actions.append(f"mcp_no_manifest:{server.name}")
            continue
        for schema in schemas:
            name = schema.get("name")
            if not name or name in seen:
                continue
            seen.add(name)

            def make(tool_name: str):
                async def handler(args: Dict[str, Any], flow_manager: Any = None):
                    # A catalogue tool gets a shaped result — an agent told to
                    # quote a product cannot be graded on {"simulated": true}.
                    if tool_name in ("search_catalog", "get_product"):
                        http_mock.calls.append((tool_name, dict(args)))
                        return _catalog_response(tool_name, args), None
                    return http_mock.response_for(tool_name, args), None

                return handler

            out.append(
                FlowsFunctionSchema(
                    name=name,
                    description=schema.get("description", ""),
                    properties=schema.get("properties") or {},
                    required=schema.get("required") or [],
                    handler=cast(Any, make(name)),
                )
            )
    return out


# ---------------------------------------------------------------------------
# Payload overrides: schema defaults < sidecar < --payload < --set
# ---------------------------------------------------------------------------


def sidecar_payload_path(template_path: Path) -> Path:
    from template_simulation.suite import PAYLOAD_FILE_SUFFIX

    return template_path.with_suffix("").with_suffix(PAYLOAD_FILE_SUFFIX)


def resolve_payload_overrides(
    *, template_path: Path, schema: Dict[str, Any]
) -> Dict[str, Any]:
    """Real values a teammate supplied. Applied ON TOP of the synthesized
    defaults, never in place of them."""
    import json

    sidecar = sidecar_payload_path(template_path)
    if not sidecar.exists():
        return {}
    data = json.loads(sidecar.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"payload file {sidecar} must contain a JSON object")

    overrides: Dict[str, Any] = {}
    for key, value in data.items():
        spec = schema.get(key) if isinstance(schema.get(key), dict) else {}
        kind = (spec or {}).get("type")
        if kind == "number" and isinstance(value, str):
            try:
                overrides[key] = float(value) if "." in value else int(value)
            except ValueError:
                overrides[key] = value
        elif kind == "boolean" and isinstance(value, str):
            overrides[key] = value.strip().lower() in ("1", "true", "yes", "y")
        else:
            overrides[key] = value
    return overrides


# ---------------------------------------------------------------------------
# The customer
# ---------------------------------------------------------------------------


class LLMPersona:
    def __init__(self, brief: str, llm: Any) -> None:
        self.brief, self.llm, self._spoken = brief, llm, 0

    async def next(self, turns: List[Turn]) -> Optional[str]:
        # Role inversion: the AGENT is this model's counterpart, so its lines
        # are "user". They carry a label because without one the model answers
        # AS the agent. The persona's own turns get none — role="assistant"
        # already says "this was you", and a weaker model echoed an inline
        # label straight into its next reply.
        transcript = [
            {
                "role": "user" if t.speaker is Speaker.AGENT else "assistant",
                "content": (
                    f"[the agent says] {t.text}"
                    if t.speaker is Speaker.AGENT
                    else t.text
                ),
            }
            for t in turns
            if t.text
        ] or [{"role": "user", "content": "(the agent has not spoken yet)"}]

        try:
            reply = await cfg.with_retry(
                lambda: self.llm.run_inference(
                    LLMContext(cast(Any, transcript)),
                    system_instruction=f"{_PERSONA_RULES}\nYour character:\n{self.brief}",
                ),
                label="persona turn",
            )
        except Exception as e:
            logger.warning(f"[sim] persona inference failed: {e}")
            return None

        text = str(reply or "").strip()
        cleaned = re.sub(r"[\[\]<>*\"'.!,]", "", text).strip().lower()
        if "<SILENT>" in text or "silen" in cleaned:
            self._spoken += 1
            return _SILENCE
        ended = "<END>" in text
        text = text.replace("<END>", "").strip()
        if self._spoken < _MIN_TURNS_BEFORE_END:
            self._spoken += 1
            return text or "Sorry, could you say that again?"
        if ended or not text:
            return None
        self._spoken += 1
        return text


def build_persona(case: SimCase, llm: Any) -> "LLMPersona":
    return LLMPersona(case.persona, llm)


async def _synthesize_locally(text: str) -> bytes:
    """Offline fallback (macOS say -> 8 kHz PCM). A harness that stops working
    because a TTS bill lapsed is a bad harness, and for the CUSTOMER side any
    intelligible speech does the job."""
    if not shutil.which("say") or not shutil.which("afconvert"):
        return b""
    with tempfile.TemporaryDirectory() as tmp:
        aiff, wav_path = f"{tmp}/p.aiff", f"{tmp}/p.wav"
        try:
            say = await asyncio.create_subprocess_exec(
                "say",
                "-v",
                "Lekha",
                text,
                "-o",
                aiff,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            if await say.wait() != 0:
                return b""
            conv = await asyncio.create_subprocess_exec(
                "afconvert",
                "-f",
                "WAVE",
                "-d",
                f"LEI16@{SAMPLE_RATE}",
                "-c",
                "1",
                aiff,
                wav_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            if await conv.wait() != 0:
                return b""
            with wave.open(wav_path, "rb") as w:
                return w.readframes(w.getnframes())
        except Exception as e:
            logger.debug(f"[sim] local TTS fallback failed: {e}")
            return b""


async def synthesize(voice_config: Any, text: str) -> bytes:
    """The customer's voice: text -> 8 kHz PCM.

    Reuses production's ``_generate_elevenlabs_audio`` on the account the
    greeting path resolves (accounts.Accounts: key and host together) rather
    than a second pipecat TTS service — those hold a websocket and expect a
    pipeline lifecycle, and a second one beside the agent's gets 403'd on
    every utterance.
    """
    from app.ai.voice.agents.breeze_buddy.accounts import Accounts
    from app.ai.voice.tts.elevenlabs import _generate_elevenlabs_audio

    voice_id = getattr(voice_config, "voice_id", None)
    if not voice_id:
        return await _synthesize_locally(text)
    try:
        account = await Accounts().get(voice_config)
        mulaw = await _generate_elevenlabs_audio(
            text=text,
            voice_id=voice_id,
            model_id=getattr(voice_config, "model", None),
            api_key=getattr(account, "api_key", None),
            base_url="https://"
            + str(getattr(account, "endpoint", "")).removeprefix("wss://"),
        )
        return audioop.ulaw2lin(mulaw, 2)
    except Exception as e:
        logger.warning(f"[sim] persona TTS failed ({e}); using local speech")
        return await _synthesize_locally(text)


# ---------------------------------------------------------------------------
# The transport
# ---------------------------------------------------------------------------


@dataclass
class _Segment:
    speaker: str
    pcm: bytes
    offset_s: float


class CallRecorder:
    """Both sides, placed on the call's own timeline, rendered as a stereo WAV
    (customer left, agent right). Positioned by when things actually happened,
    so a gap is a real silence and an overlap is a real interruption —
    concatenating in arrival order would erase exactly the timing defects this
    mode exists to find."""

    def __init__(self) -> None:
        self.segments: List[_Segment] = []
        self._t0 = time.monotonic()
        # Per-speaker write cursor: TTS streams faster than real time, so
        # arrival order alone bunches speech into bursts. Laying chunks
        # end-to-end restores the true duration; a real pause still pushes.
        self._cursor: Dict[str, float] = {}

    def reset_clock(self) -> None:
        self._t0, self._cursor = time.monotonic(), {}

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._t0

    def add(self, speaker: str, pcm: bytes, offset_s: Optional[float] = None) -> None:
        if not pcm:
            return
        if offset_s is None:
            offset_s = max(self.elapsed, self._cursor.get(speaker, 0.0))
        self._cursor[speaker] = offset_s + len(pcm) / (SAMPLE_RATE * _BYTES_PER_SAMPLE)
        self.segments.append(_Segment(speaker, pcm, offset_s))

    def duration_s(self) -> float:
        if not self.segments:
            return 0.0
        return max(
            s.offset_s + len(s.pcm) / (SAMPLE_RATE * _BYTES_PER_SAMPLE)
            for s in self.segments
        )

    def render_wav(self) -> bytes:
        if not self.segments:
            return b""
        total = int(self.duration_s() * SAMPLE_RATE) + SAMPLE_RATE
        left = bytearray(total * _BYTES_PER_SAMPLE)  # customer
        right = bytearray(total * _BYTES_PER_SAMPLE)  # agent
        for seg in self.segments:
            target = left if seg.speaker == "customer" else right
            start = int(seg.offset_s * SAMPLE_RATE) * _BYTES_PER_SAMPLE
            end = start + len(seg.pcm)
            if end > len(target):  # grow rather than truncate
                left.extend(bytearray(max(0, end - len(left))))
                right.extend(bytearray(max(0, end - len(right))))
            target[start:end] = seg.pcm

        size = min(len(left), len(right))
        interleaved = bytearray(size * 2)
        interleaved[0::4] = left[0:size:2]
        interleaved[1::4] = left[1:size:2]
        interleaved[2::4] = right[0:size:2]
        interleaved[3::4] = right[1:size:2]
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(_BYTES_PER_SAMPLE)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(bytes(interleaved))
        return buf.getvalue()


class SimAudioParams(TransportParams):
    audio_in_enabled: bool = True
    audio_out_enabled: bool = True
    audio_in_sample_rate: Optional[int] = SAMPLE_RATE
    audio_out_sample_rate: Optional[int] = SAMPLE_RATE
    audio_in_channels: int = 1
    audio_out_channels: int = 1


class _Mic(BaseInputTransport):
    """Where the customer speaks."""

    def __init__(self, params: SimAudioParams, recorder: CallRecorder) -> None:
        super().__init__(params)
        self._recorder = recorder
        self._ready = asyncio.Event()

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        await self.set_transport_ready(frame)
        self._ready.set()

    async def wait_ready(self, timeout: float = 30.0) -> None:
        await asyncio.wait_for(self._ready.wait(), timeout=timeout)

    async def _push(self, pcm: bytes, pace: bool) -> None:
        for i in range(0, len(pcm), _FRAME_BYTES):
            chunk = pcm[i : i + _FRAME_BYTES].ljust(_FRAME_BYTES, b"\x00")
            await self.push_audio_frame(
                InputAudioRawFrame(audio=chunk, sample_rate=SAMPLE_RATE, num_channels=1)
            )
            if pace:
                await asyncio.sleep(_FRAME_MS / 1000.0)

    async def speak(self, pcm: bytes) -> None:
        """Paced in real time, because that is what VAD and the turn
        strategies measure — an unpaced burst reads as impossibly fast speech
        and makes turn detection look broken when it isn't."""
        if not pcm:
            return
        self._recorder.add("customer", pcm)
        await self._push(pcm, pace=True)

    async def silence(self, seconds: float) -> None:
        await self._push(
            b"\x00" * int(SAMPLE_RATE * _BYTES_PER_SAMPLE * seconds), pace=True
        )


class _Speaker(BaseOutputTransport):
    """Where the agent is heard — recorded instead of played.

    Turn-taking is NOT decided here (see ``_Observer``). Writes are paced to
    their own real duration: a real phone line throttles, this transport does
    not, so pipecat can hand a whole utterance over in one wall-clock burst —
    and the recorder positions agent audio by duration but customer audio by
    arrival, so unpaced writes drift into overlap that never happened.
    """

    def __init__(self, params: SimAudioParams, recorder: CallRecorder) -> None:
        super().__init__(params)
        self._recorder = recorder

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        self._recorder.add("agent", frame.audio)
        duration = len(frame.audio) / (frame.sample_rate * frame.num_channels * 2)
        if duration > 0:
            await asyncio.sleep(duration)
        return True


class SimTransport(BaseTransport):
    """Shaped like every other pipecat transport, so build_pipeline cannot
    tell the difference."""

    def __init__(self) -> None:
        super().__init__()
        params = SimAudioParams()
        self.recorder = CallRecorder()
        self._input = _Mic(params, self.recorder)
        self._output = _Speaker(params, self.recorder)

    def input(self) -> _Mic:
        return self._input

    def output(self) -> _Speaker:
        return self._output

    @property
    def mic(self) -> _Mic:
        return self._input


class _Observer(BaseObserver):
    """Reconstructs the call from the frames going past. An observer, not an
    inserted processor, so measuring cannot change what is measured."""

    def __init__(self) -> None:
        super().__init__()
        self.turns: List[Turn] = []
        self.tool_calls: List[ToolCallRecord] = []
        # What STT actually delivered, kept apart from what the persona meant
        # to say. The gap between them is the finding audio mode exists for.
        self.heard: List[str] = []
        self.interruptions: List[float] = []
        self.agent_turn_done = asyncio.Event()
        self._buffer: List[str] = []
        self._t0 = time.monotonic()
        self._seen_calls: set = set()
        # A frame is observed once per processor LINK it crosses, not once —
        # an 11-processor pipeline reports the same TTSTextFrame many times.
        self._seen_frames: set = set()

    def start_clock(self) -> None:
        self._t0 = time.monotonic()

    def at(self) -> float:
        return round(time.monotonic() - self._t0, 2)

    def _first_sighting(self, frame: Any) -> bool:
        fid = getattr(frame, "name", None) or f"{type(frame).__name__}#{id(frame)}"
        if fid in self._seen_frames:
            return False
        self._seen_frames.add(fid)
        return True

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame
        if not self._first_sighting(frame):
            return

        if isinstance(frame, TTSTextFrame):
            if frame.text:
                self._buffer.append(frame.text)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.flush_agent()
            # Unconditional: the frame itself IS "the agent stopped producing
            # audio", whether or not the flush found new text.
            self.agent_turn_done.set()
        elif isinstance(frame, InterruptionFrame):
            self.interruptions.append(self.at())
        elif isinstance(frame, TranscriptionFrame):
            text = (frame.text or "").strip()
            if text:
                self.heard.append(text)
                self.turns.append(Turn(Speaker.CUSTOMER, text, at_s=self.at()))
        elif isinstance(frame, FunctionCallInProgressFrame):
            key = f"{frame.function_name}:{getattr(frame, 'tool_call_id', '')}"
            if key not in self._seen_calls:
                self._seen_calls.add(key)
                self.tool_calls.append(
                    ToolCallRecord(
                        frame.function_name,
                        dict(getattr(frame, "arguments", None) or {}),
                        at_s=self.at(),
                    )
                )
        elif isinstance(frame, FunctionCallResultFrame):
            for record in reversed(self.tool_calls):
                if record.name == frame.function_name and record.result is None:
                    record.result = str(getattr(frame, "result", ""))[:2000]
                    break

    def flush_agent(self) -> None:
        # TTS emits word-level chunks with no trailing space, so "".join
        # produced "मैंabcसे". Join on a space, collapse the doubles.
        text = re.sub(r"\s+", " ", " ".join(self._buffer)).strip()
        self._buffer.clear()
        if not text:
            return
        # pipecat fires TWO BotStoppedSpeakingFrames for one utterance if its
        # output queue goes quiet; the halves differ only in boundary
        # whitespace, so this check ignores it.
        last = next(
            (t for t in reversed(self.turns) if t.speaker is Speaker.AGENT), None
        )
        strip = lambda s: re.sub(r"\s+", "", s)  # noqa: E731
        if last is not None and strip(last.text) == strip(text):
            return
        self.turns.append(Turn(Speaker.AGENT, text, at_s=self.at()))


# ---------------------------------------------------------------------------
# Running one call
# ---------------------------------------------------------------------------


def render_vars(template: TemplateModel, payload: Dict[str, Any]) -> Dict[str, str]:
    """Template variables for {placeholder} substitution, with production's own
    transform chains applied — the agent hears "one thousand two hundred fifty
    rupees", not "1250.5", and testing it on the raw value tests nothing real."""
    schema = template.expected_payload_schema or {}
    merged = {
        k: v["example"]
        for k, v in schema.items()
        if isinstance(v, dict) and "example" in v
    }
    merged.update(payload or {})

    out: Dict[str, str] = {}
    for key, value in merged.items():
        spec = schema.get(key) if isinstance(schema.get(key), dict) else {}
        names = (spec or {}).get("function")
        for name in [names] if isinstance(names, str) else names or []:
            fn = TEMPLATE_FUNCTION_REGISTRY.get(name)
            if fn is None:
                continue
            try:
                value = fn(value)
            except Exception as e:
                logger.debug(f"[sim] transform {name} on {key} failed: {e}")
        out[key] = str(value)
    return out


def render_greeting(template: TemplateModel, tvars: Dict[str, str]) -> Optional[str]:
    cfg_model = template.configurations
    greeting = getattr(cfg_model, "initial_greeting", None) if cfg_model else None
    if not greeting or not isinstance(greeting, str):
        return None
    for key, value in tvars.items():
        greeting = greeting.replace("{" + key + "}", value)
    return greeting


@dataclass
class _Prepared:
    template: TemplateModel
    builder: FlowConfigBuilder
    flow_config: Dict[str, Any]
    mcp_funcs: List[Any] = field(default_factory=list)
    tvars: Dict[str, str] = field(default_factory=dict)


def prepare(template: TemplateModel, case: SimCase, bot: SimBot) -> _Prepared:
    """Neutralize hooks, render prompts, build the flow config, mock the
    boundary-crossing handlers, prepare MCP tools.

    Prompts are rendered here because production does it at DB-load time
    (FlowConfigLoader.load_template), which loading a template from a JSON
    file skips — and the FlowManager reads node prompts out of flow_config
    once, at start, so it has to happen before build_flow_config captures them.
    """
    http_mock = MockHttpTool()
    tvars = render_vars(template, case.payload)

    sim_template = neutralize_hooks(template, bot)
    rendered = copy.deepcopy(sim_template)
    for node in (rendered.flow or {}).get("nodes") or []:
        for name in ("task_messages", "role_messages"):
            if node.get(name):
                node[name] = render_messages_with_vars(node[name], tvars)

    builder = FlowConfigBuilder(quiet=True)
    install_handler_mocks(builder, bot, http_mock)
    # Wrap AFTER mocking and BEFORE build: _build_function_schema captures the
    # handler into a closure, so wrapping post-build is a no-op.
    for name, handler in list(builder.handler_map.items()):
        builder.handler_map[name] = with_context(bot)(handler)

    flow_config = builder.build_flow_config(rendered)
    bot.flow_config = flow_config
    return _Prepared(
        template=rendered,
        builder=builder,
        flow_config=flow_config,
        mcp_funcs=build_mcp_mocks(template, bot, http_mock),
        tvars=tvars,
    )


async def run_call(
    *, template: TemplateModel, case: SimCase, persona: LLMPersona, repetition: int = 0
) -> SimTrace:
    """Drive one call end to end and return its trace."""
    from app.ai.voice.agents.breeze_buddy.agent.flow import (
        prepare_initial_node,
        setup_flow_manager,
    )
    from app.ai.voice.agents.breeze_buddy.agent.pipeline import build_pipeline
    from app.ai.voice.agents.breeze_buddy.llm import get_llm_service
    from app.ai.voice.agents.breeze_buddy.stt import get_stt_service
    from app.ai.voice.agents.breeze_buddy.template.vad import create_vad_analyzer
    from app.ai.voice.agents.breeze_buddy.tts import (
        generate_audio,
        get_tts_service,
        resolve_voice_config,
    )

    trace = SimTrace(
        case_id=case.id, repetition=repetition, template_name=template.name
    )
    bot = SimBot(case_id=case.id, template=template, payload=case.payload)
    configurations = template.configurations
    idle_cfg = getattr(configurations, "user_idle_configuration", None)
    silence_wait = (
        idle_cfg.timeout + 1.0
        if idle_cfg and getattr(idle_cfg, "enabled", False)
        else _TRAILING_SILENCE_S
    )
    observer = _Observer()
    transport = SimTransport()
    task: Optional[PipelineTask] = None
    runner_task: Optional[asyncio.Task] = None

    try:
        prepared = prepare(template, case, bot)

        # The REAL services this template declares.
        stt = await get_stt_service(
            stt_configuration=(
                configurations.stt_configuration if configurations else None
            )
        )
        voice_config = await resolve_voice_config(
            configurations.tts_configuration if configurations else None,
            configurations.tts_configuration_overrides if configurations else None,
        )
        tts = await get_tts_service(voice_config)
        agent_config = cfg.agent_llm_config(template)
        llm = await get_llm_service(agent_config)
        trace.agent_model = getattr(agent_config, "model", None)

        # A distinct voice for the customer, always via ElevenLabs (synthesize()
        # only ever calls _generate_elevenlabs_audio) — never the agent's own
        # provider/model. A template on Google/Cartesia/etc. left voice_id AND
        # model as that provider's, so this once sent a Gemini model name to
        # ElevenLabs's API on every persona turn: a 400 on every single line.
        from app.core.config.static import ELEVENLABS_MODEL_ID

        persona_voice = copy.deepcopy(voice_config)
        persona_voice.provider = TTSProvider.ELEVENLABS
        persona_voice.credential_id = None  # the env's account, not the agent's
        persona_voice.voice_id = _PERSONA_VOICE_ID
        persona_voice.model = ELEVENLABS_MODEL_ID

        # Telephony mode, not Daily — 8 kHz plus the template's own vad_config
        # over the telephony defaults, exactly as an outbound call builds it.
        vad_analyzer, _params = await create_vad_analyzer(
            is_daily_mode=False, template=template
        )
        bot.vad_analyzer = vad_analyzer

        async def on_idle(retry_count: int) -> None:
            """Production's backstop for a caller who never comes back: after
            max_retries it ends the call as BUSY. Without mirroring it, an
            idling call loops the re-engage script forever."""
            if bot.conversation_ended:
                return
            if bot.lead:
                bot.lead.outcome = "BUSY"
                bot.lead.metaData = {
                    **(bot.lead.metaData or {}),
                    "call_end_reason": "user_idle_timeout",
                    "idle_retry_count": int(retry_count),
                }
            handler = prepared.builder.handler_map.get("end_conversation")
            if handler is None:
                return
            try:
                await handler(TemplateContext(bot), {})
            except SimTerminated:
                # Expected: the mocked terminal handler signals by raising.
                # Escaping makes production's idle wrapper keep firing idle
                # events at a call that already ended.
                pass

        pipeline, _context, aggregator, *_rest = await build_pipeline(
            transport=transport,
            stt=stt,
            llm=cast(Any, llm),
            tts=tts,
            vad_analyzer=vad_analyzer,
            configurations=configurations,
            on_user_idle_timeout=on_idle,
        )
        task = PipelineTask(
            pipeline,
            params=PipelineParams(
                audio_in_sample_rate=SAMPLE_RATE,
                audio_out_sample_rate=SAMPLE_RATE,
                allow_interruptions=True,
            ),
            observers=[observer],
        )
        bot.task = task
        runner_task = asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))

        await transport.mic.wait_ready(timeout=30)
        transport.recorder.reset_clock()
        observer.start_clock()

        # setup_flow_manager + prepare_initial_node + initialize() is the same
        # three-step sequence agent/__init__.py runs. Seeding messages by hand
        # instead would test a conversation production never has.
        greeting = render_greeting(template, prepared.tvars)
        flow_manager = setup_flow_manager(
            task=task,
            llm=cast(Any, llm),
            context_aggregator=aggregator,
            transport=transport,
            flow_builder=prepared.builder,
            template=prepared.template,
            bot_instance=bot,
            mcp_global_functions=prepared.mcp_funcs,
        )
        initial_node = prepare_initial_node(
            flow_config=prepared.flow_config,
            lead_payload=dict(case.payload),
            configurations=configurations,
            has_greeting_source=bool(greeting),
            greeting_text=greeting,
        )
        TemplateContext(bot).record_node_entry(prepared.flow_config["initial_node"])

        if greeting:
            # Production pre-synthesizes the greeting straight to the socket
            # (agent/utils.send_initial_greeting), never through the live TTS
            # path — whose output-queue timeout can replay it mid-greeting.
            mulaw = await generate_audio(
                text=greeting, voice_config=None, configurations=configurations
            )
            pcm = audioop.ulaw2lin(mulaw, 2)
            transport.recorder.add("agent", pcm)
            observer.turns.append(Turn(Speaker.AGENT, greeting, at_s=0.0))
            # Sleep its real duration, or the customer's first reply lands
            # while the greeting is still playing — a real caller cannot.
            await asyncio.sleep(len(pcm) / (SAMPLE_RATE * _BYTES_PER_SAMPLE))

            idle_cfg = getattr(configurations, "user_idle_configuration", None)
            active_task = task
            if (
                idle_cfg is not None
                and active_task is not None
                and getattr(idle_cfg, "enabled", False)
                and not has_realtime_llm(
                    getattr(configurations, "llm_configurations", None)
                )
            ):
                idle_wait_s = 5.0 + float(idle_cfg.timeout)
                idle_message = str(idle_cfg.idle_message)

                async def _post_greeting_idle() -> None:
                    await asyncio.sleep(idle_wait_s)
                    if not bot.conversation_ended and not any(
                        t.speaker is Speaker.CUSTOMER for t in observer.turns
                    ):
                        await active_task.queue_frames(
                            [
                                LLMMessagesAppendFrame(
                                    [{"role": "system", "content": idle_message}],
                                    run_llm=True,
                                )
                            ]
                        )

                asyncio.create_task(_post_greeting_idle())

        await flow_manager.initialize(initial_node)
        await _converse(
            trace,
            bot,
            transport,
            observer,
            persona,
            case,
            persona_voice,
            greeting_delivered=bool(greeting),
            silence_wait=silence_wait,
        )
    except Exception as e:
        logger.exception(f"[sim] case {case.id} crashed")
        trace.end_reason = EndReason.ERROR
        # The full traceback: a pydantic ValidationError's str() is hundreds
        # of "field invalid" lines with no indication of the call site.
        trace.error = f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}"
    finally:
        if task is not None:
            try:
                await task.queue_frame(EndFrame())
            except Exception:
                pass
        if runner_task is not None:
            runner_task.cancel()
            try:
                await runner_task
            except (asyncio.CancelledError, Exception):
                pass

    observer.flush_agent()
    trace.turns = observer.turns
    trace.tool_calls = observer.tool_calls
    trace.node_traversal = bot.node_traversal
    trace.nodes_visited = [
        e["node_name"] for e in bot.node_traversal if e.get("node_name")
    ]
    for hook in bot.recorded_hooks:
        hook.name = restore_hook_name(hook.name)
    trace.hooks = bot.recorded_hooks
    if trace.final_node is None and trace.nodes_visited:
        trace.final_node = trace.nodes_visited[-1]
    if trace.end_reason is EndReason.ERROR and not trace.error:
        trace.end_reason = EndReason.ENDED
    trace.duration_s = transport.recorder.duration_s()
    trace.stt_heard = observer.heard
    trace.interruptions = observer.interruptions
    trace.audio_wav = transport.recorder.render_wav()
    release_sim_hooks(bot)
    return trace


def _finalize(trace: SimTrace, bot: SimBot) -> None:
    trace.end_reason = (
        EndReason.TRANSFERRED if bot.end_reason == "transferred" else EndReason.ENDED
    )
    if bot.end_node:
        trace.final_node = bot.end_node


async def _converse(
    trace: SimTrace,
    bot: SimBot,
    transport: SimTransport,
    observer: _Observer,
    persona: LLMPersona,
    case: SimCase,
    persona_voice: Any,
    *,
    greeting_delivered: bool,
    silence_wait: float,
) -> None:
    """Take turns until someone ends the call.

    ``bot.conversation_ended`` is the only termination signal available: the
    SimTerminated raised by the mocked handler is swallowed inside pipecat's
    function-call executor and never reaches here. It is checked at every
    handoff so no persona audio is injected after the call is over.
    """
    started = time.monotonic()
    customer_turns = 0
    first = True

    while customer_turns < cfg.MAX_CUSTOMER_TURNS:
        if bot.conversation_ended:
            return _finalize(trace, bot)
        if time.monotonic() - started > cfg.MAX_CALL_SECONDS:
            trace.end_reason = EndReason.MAX_TURNS
            return

        # The greeting never entered the pipeline, so no BotStoppedSpeaking
        # fires for it — don't wait on a signal that cannot come.
        skip_wait, first = first and greeting_delivered, False
        interrupted = False

        if skip_wait:
            pass
        elif case.may_interrupt:
            # Give the agent a head start; if it is STILL talking, cut in for
            # real. The only way to exercise a template's own interruption
            # handling instead of politely avoiding it.
            try:
                await asyncio.wait_for(
                    observer.agent_turn_done.wait(), _BARGE_IN_DELAY_S
                )
            except asyncio.TimeoutError:
                said = await persona.next(observer.turns)
                if said and said != _SILENCE:
                    trace.persona_said.append(f"[interrupting] {said}")
                    customer_turns += 1
                    pcm = await synthesize(persona_voice, said)
                    if pcm:
                        await transport.mic.speak(pcm)  # genuinely overlaps
                        await transport.mic.silence(_TRAILING_SILENCE_S)
                        interrupted = True
                # NOT cleared: if the agent's turn concluded during speak()
                # above, that signal must not be erased.
                if not observer.agent_turn_done.is_set():
                    try:
                        await asyncio.wait_for(
                            observer.agent_turn_done.wait(), _AGENT_TURN_TIMEOUT_S
                        )
                    except asyncio.TimeoutError:
                        pass
        else:
            # Wait for pipecat's OWN end-of-turn signal, not a guess from
            # audio timing (streaming TTS gaps look like silence mid-sentence).
            try:
                await asyncio.wait_for(
                    observer.agent_turn_done.wait(), _AGENT_TURN_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                logger.debug("[sim] agent never signalled turn-done; continuing")
        observer.agent_turn_done.clear()

        # The classifying tool call can resolve a beat AFTER the spoken reply
        # is flushed — text and function-call are separate completions.
        for _ in range(20):
            if bot.conversation_ended:
                break
            await asyncio.sleep(0.05)
        if bot.conversation_ended:
            return _finalize(trace, bot)

        if interrupted:
            # The line was already spoken mid-turn; speaking again here would
            # be two customer turns in one exchange (a real counting bug once).
            continue

        said = await persona.next(observer.turns)
        if said is None:
            trace.end_reason = EndReason.PERSONA_DONE
            return
        if bot.conversation_ended:
            return _finalize(trace, bot)
        # Checked again here, not only at the top of the loop: one exchange can
        # itself run tens of seconds, and the budget is there to stop spending
        # real STT/TTS on a call that is never going to converge.
        if time.monotonic() - started > cfg.MAX_CALL_SECONDS:
            trace.end_reason = EndReason.MAX_TURNS
            return

        if said == _SILENCE:
            trace.persona_said.append("<SILENT>")
            await transport.mic.silence(silence_wait)
            continue

        customer_turns += 1
        trace.persona_said.append(said)

        pcm = await synthesize(persona_voice, said)
        if not pcm:
            trace.end_reason = EndReason.ERROR
            trace.error = "persona TTS produced no audio"
            return
        await transport.mic.speak(pcm)
        await transport.mic.silence(_TRAILING_SILENCE_S)

    trace.end_reason = EndReason.MAX_TURNS
