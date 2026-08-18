"""Pydantic contracts for evaluation-backed Guardrail configuration."""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from .models import get_guardrail_model


class FocusGuardrailConfig(BaseModel):
    """Prompt-only policy that keeps an agent within its configured task."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        False,
        description=(
            "Prepend the platform Focus policy to the agent's LLM context. "
            "This does not invoke a separate guard model."
        ),
    )


class CustomGuardrailConfig(BaseModel):
    """One platform-model Guardrail applied to input or output text."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(False, description="Whether this Guardrail is enforced.")
    prompt: str = Field(
        "",
        max_length=4000,
        description="Customer-authored criteria describing content to block.",
    )
    model_id: str = Field(
        "platform-default",
        min_length=1,
        max_length=100,
        description=(
            "Public platform model identifier. Provider credentials and endpoints "
            "are resolved internally and are never stored in evaluation_config."
        ),
    )
    redirect_message: str = Field(
        "",
        max_length=500,
        description=(
            "Deterministic message shown or spoken when the Guardrail blocks "
            "content. The guard model never authors the customer-facing response."
        ),
    )

    @model_validator(mode="after")
    def _validate_enabled_fields(self):
        if self.enabled and not self.prompt.strip():
            raise ValueError("prompt is required when a custom guardrail is enabled")
        if self.enabled and not self.redirect_message.strip():
            raise ValueError(
                "redirect_message is required when a custom guardrail is enabled"
            )
        return self


class GuardrailsConfig(BaseModel):
    """Focus, input, and output settings stored in ``evaluation_config``.

    Note: re-adding config keys to the template JSON does NOT resurrect the
    old template-level behavior — ``ConfigurationModel.guardrails`` and its
    realtime-compat validator were removed from ``template/types.py`` when
    guardrail config moved to the ``evaluation_config`` table, so template
    payloads silently ignore them.
    """

    model_config = ConfigDict(extra="forbid")

    focus: FocusGuardrailConfig = Field(default_factory=FocusGuardrailConfig)
    input: Optional[CustomGuardrailConfig] = Field(
        None,
        description=(
            "Optional Guardrail for each finalized conversational user turn. "
            "Typed DIRECT UI intents use their separately validated action policy."
        ),
    )
    output: Optional[CustomGuardrailConfig] = Field(
        None,
        description=(
            "Optional Guardrail for each generated conversational assistant "
            "sentence. Tool authorization and structured UI payload validation "
            "remain separate controls."
        ),
    )
    _evaluation_config_id: Optional[str] = PrivateAttr(default=None)
    _configuration_revision: Optional[str] = PrivateAttr(default=None)

    @property
    def evaluation_config_id(self) -> Optional[str]:
        """Database identity of the configuration used by this runtime copy."""
        return self._evaluation_config_id

    def attach_evaluation_config_id(self, value: Optional[str]) -> None:
        """Attach runtime provenance without exposing it in the public config JSON."""
        self._evaluation_config_id = value

    @property
    def configuration_revision(self) -> Optional[str]:
        """Stable hash of the persisted configuration used by this runtime copy."""
        return self._configuration_revision

    def attach_configuration_revision(self, value: Optional[str]) -> None:
        """Attach immutable configuration provenance for result segmentation."""
        self._configuration_revision = value

    def has_enabled_custom_guardrails(self) -> bool:
        """Return whether input or output Guardrail enforcement is enabled."""
        return any(
            config is not None and config.enabled
            for config in (self.input, self.output)
        )

    @model_validator(mode="after")
    def _validate_custom_model_ids(self):
        enabled = [
            (direction, config)
            for direction, config in (("input", self.input), ("output", self.output))
            if config is not None and config.enabled
        ]
        if not enabled:
            return self

        for direction, config in enabled:
            definition = get_guardrail_model(config.model_id)
            supported = (
                definition.supports_input
                if direction == "input"
                else definition.supports_output
            )
            if not supported:
                raise ValueError(
                    f"guardrail model_id '{config.model_id}' does not support "
                    f"{direction} evaluation"
                )
        return self


__all__ = [
    "CustomGuardrailConfig",
    "FocusGuardrailConfig",
    "GuardrailsConfig",
]
