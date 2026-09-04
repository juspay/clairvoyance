"""Platform-owned model catalog for custom guardrail evaluation.

Evaluation configuration stores only a public ``model_id``. Provider endpoints,
deployment names, and credential references stay in this backend-owned registry.
Adding a Qwen or similar lightweight classifier later therefore does not change
the storage schema or Loom UI contract.
"""

from dataclasses import dataclass
from typing import Dict

from app.ai.voice.llm.types import LLMConfiguration, LLMProvider


@dataclass(frozen=True)
class GuardrailModelDefinition:
    id: str
    label: str
    configuration: LLMConfiguration
    supports_input: bool = True
    supports_output: bool = True


_MODEL_CATALOG: Dict[str, GuardrailModelDefinition] = {
    "platform-default": GuardrailModelDefinition(
        id="platform-default",
        label="gpt-4o-automatic (Platform default)",
        configuration=LLMConfiguration(
            provider=LLMProvider.AZURE,
            model="gpt-4o-automatic",
            temperature=0.0,
            max_tokens=128,
        ),
    ),
}


def get_guardrail_model(model_id: str) -> GuardrailModelDefinition:
    """Resolve a public model ID without exposing provider credentials."""
    try:
        return _MODEL_CATALOG[model_id]
    except KeyError as exc:
        available = ", ".join(sorted(_MODEL_CATALOG))
        raise ValueError(
            f"Unknown guardrail model_id '{model_id}'. Available: {available}"
        ) from exc


def get_guardrail_model_options() -> list[dict[str, object]]:
    """Return the sanitized catalog contract consumed by Loom."""
    return [
        {
            "id": definition.id,
            "label": definition.label,
            "supports": [
                direction
                for direction, supported in (
                    ("input", definition.supports_input),
                    ("output", definition.supports_output),
                )
                if supported
            ],
            "is_default": definition.id == "platform-default",
        }
        for definition in _MODEL_CATALOG.values()
    ]
