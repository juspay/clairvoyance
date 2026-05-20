"""Pure helpers shared between the voice template loader and the chat agent."""

from typing import Dict

from app.ai.voice.agents.breeze_buddy.template.types import (
    FlowMode,
    KnowledgeBaseMode,
    TemplateModel,
)
from app.core.logger import logger


def render_messages_with_vars(messages: list, variables: Dict[str, str]) -> list:
    """Replace ``{key}`` placeholders in each message's ``content`` from
    ``variables``. Pure function — used by both the voice loader and the
    chat agent so the substitution logic stays in one place.
    """
    rendered_messages = []
    for message in messages:
        if isinstance(message, dict) and "content" in message:
            content = message["content"]
            for key, value in variables.items():
                placeholder = f"{{{key}}}"
                content = content.replace(placeholder, str(value))
            rendered_message = message.copy()
            rendered_message["content"] = content
            rendered_messages.append(rendered_message)
        else:
            rendered_messages.append(message)
    return rendered_messages


def validate_template_compat(template: TemplateModel) -> None:
    """Reject incompatible combinations between flow mode and LLM mode.

    Realtime / speech-to-speech LLMs (set via
    ``configurations.llm_configurations.realtime``) are constrained in two
    ways for v1:

    1. They are only supported in direct-mode templates — flow-mode relies
       on per-node prompt/tool swapping which doesn't translate cleanly to
       the realtime session model (one persistent session.update channel).
    2. They cannot be combined with ``configurations.keyword_filter`` —
       the keyword filter is enforced inside ``TranscriptionGateProcessor``,
       which the realtime pipeline topology omits (audio in/out, server-
       side STT and turn detection live inside the realtime LLM service,
       so there is no place in the pipeline to drop matched transcripts).
       Reject loudly rather than silently letting the filter no-op.
    """
    flow = template.flow or {}
    configurations = template.configurations
    llm_config = (
        configurations.llm_configurations if configurations is not None else None
    )
    realtime = llm_config.realtime if llm_config else None
    if realtime is None:
        return

    flow_mode = flow.get("mode")
    if flow_mode != FlowMode.DIRECT.value:
        raise ValueError(
            "Realtime LLMs (llm_configurations.realtime set) are only "
            "supported with direct-mode templates. Set flow.mode='direct' "
            f"on template {template.id} or disable realtime."
        )

    keyword_filter = (
        getattr(configurations, "keyword_filter", None)
        if configurations is not None
        else None
    )
    if keyword_filter is not None and getattr(keyword_filter, "enabled", False):
        raise ValueError(
            "Realtime LLMs (llm_configurations.realtime set) cannot be "
            "combined with configurations.keyword_filter — the realtime "
            "pipeline has no TranscriptionGateProcessor to enforce it. "
            f"Disable keyword_filter on template {template.id} or disable "
            "realtime."
        )

    wake_phrase = (
        getattr(configurations, "wake_phrase", None)
        if configurations is not None
        else None
    )
    if wake_phrase is not None and getattr(wake_phrase, "enabled", False):
        raise ValueError(
            "Realtime LLMs (llm_configurations.realtime set) cannot be "
            "combined with configurations.wake_phrase — the realtime pipeline "
            "builds its own turn strategies and WakePhraseUserTurnStartStrategy "
            f"is never installed. Disable wake_phrase on template {template.id} "
            "or disable realtime."
        )

    kb_config = (
        getattr(configurations, "knowledge_base", None)
        if configurations is not None
        else None
    )
    if (
        kb_config is not None
        and getattr(kb_config, "enabled", False)
        and kb_config.mode in (KnowledgeBaseMode.AUTO, KnowledgeBaseMode.AUTO_RETRIEVE)
    ):
        raise ValueError(
            "Realtime LLMs (llm_configurations.realtime set) cannot be "
            "combined with knowledge_base mode 'auto'/'auto_retrieve' — the "
            "realtime pipeline has no pre-LLM text hop for per-turn retrieval "
            f"injection. Use mode='full_injection' or 'tool' on template "
            f"{template.id}, or disable realtime."
        )

    logger.info(
        f"Template {template.id} validated: realtime LLM + direct mode "
        f"(provider={realtime.provider.value})"
    )
