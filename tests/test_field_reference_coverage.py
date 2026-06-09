"""
Drift tests: field_reference.json stays in sync with all Breeze Buddy template models.

Structure expected in field_reference.json
-------------------------------------------
Each top-level key that matches a Pydantic class name in types.py is a model
section. Inside, each key that does not start with '_' is a field entry that
must have:
  - "description": a non-empty string
  - "example":     any value (including null)

Subclass sections declare "_parent": "ParentClass" — only the fields the
subclass adds beyond the parent are documented there (the rest live in the
parent section). The test automatically computes the delta.

Keys starting with '_' (e.g. "_parent", "_description") are metadata and are
ignored by the tests. Non-model top-level keys (e.g. "sample_template") are
silently skipped because they cannot be imported as a class from types.py.

When to run
-----------
- Automatically at commit time via .githooks/pre-commit.
- Manually: uv run pytest tests/test_field_reference_coverage.py -q

When the build fails
--------------------
1. New field added to a model but missing from the section → add it with
   'description' and 'example'.
2. Field removed from a model but stale entry remains → remove it.
3. Existing entry missing 'description' or 'example' → add the missing key(s).
"""

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

REFERENCE_PATH = (
    Path(__file__).parent.parent
    / "app/ai/voice/agents/breeze_buddy/template/field_reference.json"
)

TYPES_MODULE = "app.ai.voice.agents.breeze_buddy.template.types"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _import_model(class_name: str) -> Any:
    module = importlib.import_module(TYPES_MODULE)
    return getattr(module, class_name)


def _is_deprecated(field_info: Any) -> bool:
    desc = field_info.description or ""
    return "DEPRECATED" in desc or desc.lower().startswith("deprecated")


def _json_facing_fields(model_class: Any, parent_class: Any = None) -> dict[str, Any]:
    """
    Return JSON-facing field names for a model, optionally subtracting parent fields.

    - Resolves aliases (e.g. func_pre_actions → pre_actions).
    - Skips fields with exclude=True (e.g. compiled_handler).
    - Skips deprecated fields.
    - When parent_class is given, removes fields already in the parent so
      subclass sections only document the delta.
    """
    result: dict[str, Any] = {}
    for name, info in model_class.model_fields.items():
        if getattr(info, "exclude", False):
            continue
        if _is_deprecated(info):
            continue
        json_name = info.alias if info.alias else name
        result[json_name] = info

    if parent_class is not None:
        parent_names = {
            (info.alias if info.alias else name)
            for name, info in parent_class.model_fields.items()
            if not getattr(info, "exclude", False)
        }
        result = {k: v for k, v in result.items() if k not in parent_names}

    return result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def reference() -> dict:
    with open(REFERENCE_PATH) as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def model_sections(reference: dict) -> dict[str, tuple[dict, dict]]:
    """
    For every top-level key in the reference that matches a Pydantic class name,
    return a dict mapping:

        class_name -> (expected_model_fields, actual_reference_fields)
    """
    result: dict[str, tuple[dict, dict]] = {}

    for key, section in reference.items():
        if not isinstance(section, dict):
            continue
        # Try to import as a model class; skip if not found.
        try:
            model_class = _import_model(key)
        except AttributeError:
            continue

        parent_class = None
        if "_parent" in section:
            parent_class = _import_model(section["_parent"])

        skip = set(section.get("_skip", []))
        model_fields = {
            k: v
            for k, v in _json_facing_fields(model_class, parent_class).items()
            if k not in skip
        }
        # Strip metadata keys (_parent, _skip, _description, …) to get only field entries.
        ref_fields = {k: v for k, v in section.items() if not k.startswith("_")}

        result[key] = (model_fields, ref_fields)

    return result


# ---------------------------------------------------------------------------
# Test 1: every model field is present in the section
# ---------------------------------------------------------------------------


def test_all_fields_present_in_reference(reference: dict, model_sections: dict) -> None:
    """Every non-deprecated, non-excluded model field must appear in its section."""
    missing: list[str] = []
    for class_name, (model_fields, ref_fields) in model_sections.items():
        for field in sorted(set(model_fields) - set(ref_fields)):
            missing.append(f"  {class_name}.{field}")

    assert not missing, (
        f"{len(missing)} field(s) exist in models but are missing from "
        f"field_reference.json — add them with 'description' and 'example':\n"
        + "\n".join(missing)
    )


# ---------------------------------------------------------------------------
# Test 2: no stale entries in the section
# ---------------------------------------------------------------------------


def test_no_stale_fields_in_reference(reference: dict, model_sections: dict) -> None:
    """No section entry should reference a field removed from the model."""
    stale: list[str] = []
    for class_name, (model_fields, ref_fields) in model_sections.items():
        for field in sorted(set(ref_fields) - set(model_fields)):
            stale.append(f"  {class_name}.{field}")

    assert not stale, (
        f"{len(stale)} stale field(s) in field_reference.json no longer exist "
        f"in the corresponding models — remove them:\n" + "\n".join(stale)
    )


# ---------------------------------------------------------------------------
# Test 3: every entry has both 'description' and 'example'
# ---------------------------------------------------------------------------


def test_all_entries_have_description_and_example(
    reference: dict, model_sections: dict
) -> None:
    """Every field entry must have a non-empty 'description' and an 'example'."""
    incomplete: list[str] = []

    for class_name, (model_fields, ref_fields) in model_sections.items():
        for field_name, entry in ref_fields.items():
            if field_name not in model_fields:
                continue  # stale — reported by test_no_stale_fields_in_reference

            missing_keys: list[str] = []
            if not entry.get("description"):
                missing_keys.append("'description'")
            if "example" not in entry:
                missing_keys.append("'example'")

            if missing_keys:
                incomplete.append(
                    f"  {class_name}.{field_name}: missing {', '.join(missing_keys)}"
                )

    assert not incomplete, (
        f"{len(incomplete)} field(s) in field_reference.json are incomplete:\n"
        + "\n".join(incomplete)
    )
