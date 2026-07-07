"""
Decoder functions for templates.
"""

import json
from typing import Any, Dict, List, Optional

import asyncpg

from app.ai.voice.agents.breeze_buddy.template.types import (
    LEGACY_VOICE_TO_PROVIDER,
    ConfigurationModel,
    TemplateModel,
)
from app.core.logger import logger


def _to_provider_config(provider: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    """Build a voice_config dict from a legacy provider-specific config."""
    cfg: Dict[str, Any] = {"provider": provider}
    if provider == "cartesia":
        cfg["voice_id"] = raw.get("voice_id")
        cfg["volume"] = raw.get("volume")
        cfg["speed"] = raw.get("speed")
        cfg["emotion"] = raw.get("emotion")
        cfg["language"] = raw.get("language")
    elif provider == "elevenlabs":
        cfg["voice_id"] = raw.get("voice_id")
        cfg["model"] = raw.get("model_id")
        cfg["speed"] = raw.get("speed")
        cfg["language"] = raw.get("language")
    return {k: v for k, v in cfg.items() if v is not None}


def _migrate_legacy_voice_config(data: Dict[str, Any]) -> Dict[str, Any]:
    """Convert old-format voice fields to the new unified tts_configuration.

    Handles templates stored with the old schema:
      - tts_voice_name: "rhea" / "mira" / "sara"
      - mira_voice_id: str
      - cartesia_voice_configurations: {...}
      - elevenlabs_voice_configurations: {...}

    Transforms them into tts_configuration + tts_configuration_overrides.
    Already-migrated templates (with tts_configuration) pass through unchanged.
    """
    if "tts_configuration" in data and data["tts_configuration"]:
        # Already in new format — strip old fields if present
        for old_key in [
            "tts_voice_name",
            "mira_voice_id",
            "cartesia_voice_configurations",
            "elevenlabs_voice_configurations",
        ]:
            data.pop(old_key, None)
        return data

    # Determine default provider from tts_voice_name
    old_voice_name = data.pop("tts_voice_name", None)
    default_provider = None
    if old_voice_name:
        name = (
            old_voice_name.lower()
            if isinstance(old_voice_name, str)
            else old_voice_name
        )
        default_provider = LEGACY_VOICE_TO_PROVIDER.get(name)
        logger.warning(
            f"[Deprecated] field 'tts_voice_name' is set (value: '{name}'). "
            "Use 'tts_configuration.provider' instead."
        )

    # Extract provider-specific configs
    old_cartesia = data.pop("cartesia_voice_configurations", None)
    old_elevenlabs = data.pop("elevenlabs_voice_configurations", None)
    old_mira_voice_id = data.pop("mira_voice_id", None)

    if old_cartesia:
        logger.warning(
            "[Deprecated] field 'cartesia_voice_configurations' is set. "
            "Use 'tts_configuration_overrides.cartesia' instead."
        )
    if old_elevenlabs:
        logger.warning(
            "[Deprecated] field 'elevenlabs_voice_configurations' is set. "
            "Use 'tts_configuration_overrides.elevenlabs' instead."
        )
    if old_mira_voice_id:
        logger.warning(
            "[Deprecated] field 'mira_voice_id' is set. "
            "Use 'tts_configuration_overrides.cartesia.voice_id' instead."
        )

    provider_configs: Dict[str, Dict[str, Any]] = {}

    if old_cartesia and isinstance(old_cartesia, dict):
        provider_configs["cartesia"] = _to_provider_config("cartesia", old_cartesia)
    if old_elevenlabs and isinstance(old_elevenlabs, dict):
        provider_configs["elevenlabs"] = _to_provider_config(
            "elevenlabs", old_elevenlabs
        )

    # Legacy mira_voice_id → cartesia voice_id fallback
    if old_mira_voice_id:
        cartesia_cfg = provider_configs.setdefault("cartesia", {"provider": "cartesia"})
        cartesia_cfg.setdefault("voice_id", old_mira_voice_id)

    # Build tts_configuration from default provider
    if not default_provider:
        # Infer from whichever provider config exists
        default_provider = next(iter(provider_configs), None)

    if default_provider:
        # Use the default provider's config as tts_configuration
        data["tts_configuration"] = provider_configs.pop(
            default_provider, {"provider": default_provider}
        )
        # Remaining provider configs become overrides
        if provider_configs:
            data["tts_configuration_overrides"] = provider_configs
            logger.debug(
                f"Migrated legacy voice config with overrides: {list(provider_configs.keys())}"
            )
        else:
            logger.debug(f"Migrated legacy voice config: provider={default_provider}")
    else:
        data["tts_configuration"] = None

    return data


def decode_template(result: asyncpg.Record) -> Optional[TemplateModel]:
    """
    Decode a single template from database result.
    """
    if not result:
        return None

    # Parse flow from JSON
    flow_data = result.get("flow", {})

    if flow_data and isinstance(flow_data, str):
        # If it's a string, parse it
        flow_data = json.loads(flow_data)

    # Parse expected_payload_schema from JSON
    expected_payload_schema_data = result.get("expected_payload_schema")

    # Parse expected_callback_response_schema from JSON
    expected_callback_response_schema_data = result.get(
        "expected_callback_response_schema"
    )

    if expected_payload_schema_data and isinstance(expected_payload_schema_data, str):
        # If it's a string, parse it
        expected_payload_schema_data = json.loads(expected_payload_schema_data)

    if expected_callback_response_schema_data and isinstance(
        expected_callback_response_schema_data, str
    ):
        # If it's a string, parse it
        expected_callback_response_schema_data = json.loads(
            expected_callback_response_schema_data
        )

    # Parse configurations from JSONB
    configurations_data = result.get("configurations")
    configurations = None

    if configurations_data:
        if isinstance(configurations_data, str):
            # If it's a string, parse it
            configurations_data = json.loads(configurations_data)

        # Migrate old voice fields to new unified voice_config
        configurations_data = _migrate_legacy_voice_config(configurations_data)

        # Create ConfigurationModel from the parsed data
        configurations = ConfigurationModel(**configurations_data)

    # Parse secrets from JSONB (can be str, dict, or None)
    secrets_data = result.get("secrets")
    if secrets_data:
        if isinstance(secrets_data, str):
            secrets_data = json.loads(secrets_data)
        # If it's already a dict (from JSONB), use it directly

    # supported_channels is a Postgres text[] (NOT NULL DEFAULT {'voice'}).
    # Some legacy SELECTs may not include the column — fall back to ['voice']
    # so the model always has at least one channel.
    supported_channels = result.get("supported_channels") or ["voice"]

    data_sources_data = result.get("data_sources")
    if data_sources_data and isinstance(data_sources_data, str):
        data_sources_data = json.loads(data_sources_data)

    return TemplateModel(
        id=str(result["id"]),
        reseller_id=result["reseller_id"],
        merchant_id=result["merchant_id"],
        name=result["name"],
        flow=flow_data,
        expected_payload_schema=expected_payload_schema_data,
        expected_callback_response_schema=expected_callback_response_schema_data,
        configurations=configurations,
        secrets=secrets_data,
        outbound_number_id=(
            str(result["outbound_number_id"])
            if result.get("outbound_number_id")
            else None
        ),
        is_active=result["is_active"],
        supported_channels=list(supported_channels),
        data_sources=data_sources_data,
        created_at=result["created_at"],
        updated_at=result["updated_at"],
    )


def decode_templates(result: List[asyncpg.Record]) -> List[TemplateModel]:
    """
    Decode templates from database result.
    """
    if not result:
        return []

    templates: List[TemplateModel] = []
    for row in result:
        t = decode_template(row)
        if t is not None:
            templates.append(t)
    return templates
