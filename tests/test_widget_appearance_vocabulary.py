"""Drift test: ``WidgetAppearance`` stays in sync with the appearance vocabulary.

The appearance vocabulary used to live as four hand-maintained lists of the
same words -- loom's ``WIDGET_APPEARANCE_KEYS``, this schema, and the two
nautilus storefront loaders. They drifted: ``theme``, ``surface_color``,
``text_color`` and ``user_bubble_color`` were missing from three of them
until 2026-09-11, which meant dark mode could not reach a Shopify storefront
at all and nothing failed. The widget token contract (loom,
``docs/WIDGET-TOKEN-CONTRACT.md`` Sec. 6) is the answer: one declaration, and
a test per repo that fails when its own structures disagree with it.

The declaration is authored in loom and copied verbatim into this repo (three
separate git repos, so there is no import that can span them). This file
checks ``WidgetAppearance`` against the copy AND pins ``revision``, so a
half-finished sync fails the build instead of shipping a silent drift.

When this fails
---------------
1. Key sets differ -> either the schema is missing a key the rest of the
   system already speaks, or it carries one nobody else knows about. The
   failure names both sides; decide which one is wrong before editing.
2. ``max_length`` differs -> the cap is part of the contract (a value that
   fits loom's snippet must survive the appearance PUT).
3. ``revision`` differs -> the vocabulary changed and the copy here was not
   re-synced, or it was and this constant was not bumped.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Union, get_args, get_origin

import pytest
from pydantic import ValidationError

from app.schemas.breeze_buddy.widget_config import WidgetAppearance

VOCABULARY_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "schemas"
    / "breeze_buddy"
    / "widget_appearance_vocabulary.json"
)

# Pinned on purpose. Bumping the vocabulary means bumping this constant --
# and its twin in loom and nautilus -- as a deliberate act, in the same
# change that re-syncs the copies. A pin that moves on its own is not a pin.
VOCABULARY_REVISION = "2026-09-11"

# One plausible value per ``role``, short enough to clear every ``maxLength``
# that role is used with. The round-trip test builds a full appearance object
# out of these, so they double as "what a merchant actually sends".
ROLE_SAMPLES: Dict[str, str] = {
    "colour": "#112233",
    "enum": "dark",
    "flag": "false",
    "length": "24px",
    "text": "Ask Zodiac",
    "url": "https://cdn.example.com/logo.png",
}


def _vocabulary() -> Dict[str, Any]:
    return json.loads(VOCABULARY_PATH.read_text(encoding="utf-8"))


def _entries() -> List[Dict[str, Any]]:
    return _vocabulary()["entries"]


def _keys_with_role(role: str) -> List[str]:
    return [entry["key"] for entry in _entries() if entry["role"] == role]


def _max_length(field: Any) -> Optional[int]:
    """Pydantic v2 keeps ``max_length`` in ``FieldInfo.metadata``."""
    for constraint in field.metadata:
        cap = getattr(constraint, "max_length", None)
        if cap is not None:
            return cap
    return None


def _sample_for(entry: Dict[str, Any]) -> str:
    value = ROLE_SAMPLES[entry["role"]]
    assert len(value) <= entry["maxLength"], (
        f"ROLE_SAMPLES[{entry['role']!r}] is longer than the maxLength of "
        f"{entry['key']!r} ({entry['maxLength']}) -- pick a shorter sample."
    )
    return value


def test_vocabulary_revision_is_pinned() -> None:
    assert _vocabulary()["revision"] == VOCABULARY_REVISION, (
        "The vocabulary copy in this repo is not the revision this test was "
        "written against. Re-sync the copy (node scripts/sync-widget-"
        "vocabulary.mjs in loom) and bump VOCABULARY_REVISION here, in loom "
        "and in nautilus together."
    )


def test_fields_are_exactly_the_vocabulary_in_order() -> None:
    expected = [entry["key"] for entry in _entries()]
    actual = list(WidgetAppearance.model_fields)

    missing = [key for key in expected if key not in actual]
    extra = [key for key in actual if key not in expected]

    # Naming the two sides separately is the whole point: "they differ" does
    # not tell you whether the schema lags the vocabulary or leads it.
    assert not missing and not extra, (
        "WidgetAppearance disagrees with the appearance vocabulary.\n"
        f"  in the vocabulary but NOT on WidgetAppearance: {missing or 'none'}\n"
        f"  on WidgetAppearance but NOT in the vocabulary: {extra or 'none'}"
    )
    # Order is checked separately so a pure reordering does not masquerade as
    # a missing key. It matters because the field order is the order the
    # console form, the snippet and the loader all read in.
    assert actual == expected, (
        "WidgetAppearance has the right keys in the wrong order.\n"
        f"  vocabulary: {expected}\n"
        f"  schema:     {actual}"
    )


@pytest.mark.parametrize("entry", _entries(), ids=lambda e: e["key"])
def test_max_length_matches_vocabulary(entry: Dict[str, Any]) -> None:
    field = WidgetAppearance.model_fields[entry["key"]]
    assert _max_length(field) == entry["maxLength"], (
        f"{entry['key']}: vocabulary says max_length={entry['maxLength']}, "
        f"schema says {_max_length(field)}"
    )


@pytest.mark.parametrize("entry", _entries(), ids=lambda e: e["key"])
def test_every_field_is_optional_and_defaults_to_none(entry: Dict[str, Any]) -> None:
    """Absent must mean "widget default" -- the schema's own docstring says so.

    A required field here would make the appearance PUT reject a merchant who
    has simply never opened the Appearance tab; a non-None default would
    write a value nobody chose into the row (map Sec. 6: that is exactly how
    ``surfaceColor: '#FFFFFF'`` got pinned into live rows and broke dark mode).
    """
    field = WidgetAppearance.model_fields[entry["key"]]
    annotation = field.annotation

    assert get_origin(annotation) is Union and type(None) in get_args(
        annotation
    ), f"{entry['key']}: annotation is {annotation!r}, expected Optional[...]"
    assert not field.is_required(), f"{entry['key']} is required"
    assert (
        field.default is None
    ), f"{entry['key']} defaults to {field.default!r}, expected None"


@pytest.mark.parametrize("key", _keys_with_role("url"), ids=lambda k: k)
def test_url_fields_enforce_https(key: str) -> None:
    """The https guard is wired to exactly the role=="url" entries.

    The list is derived from the vocabulary rather than copied, so adding a
    url-role key without adding it to ``_https_only`` fails here instead of
    shipping a schema that happily stores ``http://`` on a storefront.
    """
    with pytest.raises(ValidationError) as excinfo:
        WidgetAppearance(**{key: "http://cdn.example.com/logo.png"})
    assert key in str(excinfo.value)

    accepted = WidgetAppearance(**{key: "https://cdn.example.com/logo.png"})
    assert getattr(accepted, key) == "https://cdn.example.com/logo.png"


@pytest.mark.parametrize(
    "key", [e["key"] for e in _entries() if e["role"] != "url"], ids=lambda k: k
)
def test_non_url_fields_are_not_https_guarded(key: str) -> None:
    """The other half of "exactly": nothing else may be URL-validated.

    ``draggable`` caps at 8 characters, so the probe is 8 characters long --
    a length failure here would look like a false pass of the https guard.
    """
    assert WidgetAppearance(**{key: "http://x"}) is not None


def test_full_appearance_survives_a_round_trip() -> None:
    """Contract Sec. 7 guard 4, the "survives the appearance PUT" half.

    Every key set to a plausible value for its role must come back out of
    ``model_dump(exclude_none=True)`` -- the exact shape the PUT stores and
    the storefront resolve serves verbatim.
    """
    entries = _entries()
    payload = {entry["key"]: _sample_for(entry) for entry in entries}

    dumped = WidgetAppearance(**payload).model_dump(exclude_none=True)

    dropped = [key for key in payload if key not in dumped]
    assert not dropped, f"keys did not survive the round trip: {dropped}"
    assert dumped == payload
