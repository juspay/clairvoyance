"""Field enrichment loader + applier.

Reads ``docs/blueprint/blueprint_field_enrichment.yaml`` and copies the
``rationale`` / ``recommendation`` / ``example_phrasings`` entries onto
matching :class:`FieldNode`s. Missing entries fall back to the node's
Pydantic description + default (no crash).

Sub-schema fields are enriched with the same lookup keyed by their
prefixed path (e.g. ``FlowNodeModel.node_name``) if present.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.ai.text.agents.blueprint.schema.models import (
    FieldNode,
    Recommendation,
    RecommendationAlternative,
    SubSchema,
)
from app.core.logger import logger

# Repo-relative path to the enrichment YAML. Overridable via env var for
# tests / CI so we never silently depend on a hard-coded path.
_DEFAULT_PATH = "docs/blueprint/blueprint_field_enrichment.yaml"


@lru_cache(maxsize=1)
def load_enrichment() -> dict[str, dict[str, Any]]:
    """Parse the enrichment YAML once per process.

    Returns an empty dict if the file is missing or unparseable — the
    schema graph still works, just without curated rationale /
    recommendations.
    """
    path = os.environ.get("BLUEPRINT_FIELD_ENRICHMENT_PATH") or _DEFAULT_PATH
    file = _resolve_path(path)
    if not file.exists():
        logger.warning(
            f"Blueprint enrichment file not found at {file}; "
            "continuing without enrichment."
        )
        return {}
    try:
        raw = yaml.safe_load(file.read_text()) or {}
    except yaml.YAMLError as exc:
        logger.error(f"Failed to parse {file}: {exc}")
        return {}
    if not isinstance(raw, dict):
        logger.error(f"Enrichment YAML root must be a mapping; got {type(raw)}")
        return {}
    return raw


def apply_enrichment(
    fields: list[FieldNode],
    sub_schemas: dict[str, SubSchema] | None = None,
) -> None:
    """Mutate ``fields`` (and any sub-schema fields) in place with enrichment.

    Called once during :func:`build_schema_graph` so downstream consumers
    see enriched nodes. In-place mutation is deliberate — FieldNodes are
    otherwise built by the introspector in one pass.
    """
    data = load_enrichment()
    if not data:
        return

    for node in fields:
        _apply_one(node, data.get(node.path))

    if sub_schemas:
        for sub in sub_schemas.values():
            for node in sub.fields:
                _apply_one(node, data.get(node.path))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _apply_one(node: FieldNode, entry: dict[str, Any] | None) -> None:
    if not entry:
        return
    rationale = entry.get("rationale")
    if isinstance(rationale, str) and rationale.strip():
        node.rationale = rationale.strip()

    rec = entry.get("recommendation")
    if isinstance(rec, dict) and "value" in rec and "justification" in rec:
        node.recommendation = Recommendation(
            value=rec["value"],
            justification=str(rec["justification"]).strip(),
        )

    alternatives = entry.get("alternatives")
    if isinstance(alternatives, list):
        parsed: list[RecommendationAlternative] = []
        for alt in alternatives:
            if not isinstance(alt, dict):
                continue
            if "value" not in alt or "when" not in alt or "justification" not in alt:
                continue
            parsed.append(
                RecommendationAlternative(
                    value=alt["value"],
                    when=str(alt["when"]).strip(),
                    justification=str(alt["justification"]).strip(),
                )
            )
        node.recommendation_alternatives = parsed

    phrasings = entry.get("example_phrasings")
    if isinstance(phrasings, list):
        node.example_phrasings = [str(p).strip() for p in phrasings if p]


def _resolve_path(path: str) -> Path:
    """Resolve ``path`` as either absolute or relative to the repo root.

    The repo root is inferred by walking up from this module's location
    until we find the ``docs/`` directory — avoids depending on the cwd
    the process was launched from.
    """
    p = Path(path)
    if p.is_absolute():
        return p
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "docs").is_dir():
            return parent / p
    return p  # last-resort relative fallback


__all__ = ["apply_enrichment", "load_enrichment"]
