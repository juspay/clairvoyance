"""Load persisted Guardrails independently from template configuration."""

from typing import Optional

from app.ai.voice.agents.breeze_buddy.template.types import ConfigurationModel
from app.core.logger import logger

from .cache import get_guardrail_config_cached
from .types import GuardrailsConfig


def validate_guardrail_runtime_compat(
    configurations: Optional[ConfigurationModel],
    guardrails: GuardrailsConfig,
    *,
    template_id: str,
    supported_channels: Optional[list[str]] = None,
) -> None:
    """Reject unsupported voice-only realtime custom text gates.

    Hybrid templates may enforce the same configuration on chat while their
    realtime speech-to-speech channel skips custom input/output processors.
    """
    llm = configurations.llm_configurations if configurations else None
    if (
        llm is not None
        and llm.realtime is not None
        and guardrails.has_enabled_custom_guardrails()
        and "chat" not in (supported_channels or [])
    ):
        raise ValueError(
            "custom input/output guardrails cannot be combined with a "
            "realtime LLM (llm_configurations.realtime) — they require "
            f"finalized STT text and a pre-TTS response gate on template {template_id}"
        )


async def load_guardrail_config(
    template_id: str,
    configurations: Optional[ConfigurationModel],
    *,
    supported_channels: Optional[list[str]] = None,
) -> GuardrailsConfig:
    """Return the authoritative Guardrails without mutating template state."""
    guardrails = await get_guardrail_config_cached(template_id)

    validate_guardrail_runtime_compat(
        configurations,
        guardrails,
        template_id=template_id,
        supported_channels=supported_channels,
    )

    logger.info(
        "Loaded runtime Guardrails: "
        f"template_id={template_id} source=evaluation_config "
        f"focus_enabled={guardrails.focus.enabled} "
        f"input_enabled={bool(guardrails.input and guardrails.input.enabled)} "
        f"output_enabled={bool(guardrails.output and guardrails.output.enabled)}"
    )
    return guardrails
