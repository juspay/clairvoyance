"""Lead creation — template_id guard (``require_template_link``).

Resolution is id-only (PRs #888/#889), so every new ``lead_call_tracker``
row must carry ``template_id``. The ONLY sanctioned exceptions are the two
inbound placeholders (census 2026-07-14 found ~23k IVR-menu-abandon rows/30d
on nammayatri — legitimate, but nothing else may mint name-only leads):

- ``IVR-OPTIONS``: multi-template number, caller still in the digit menu
- ``unknown``: blocked call on a number with no template mapping

The guard runs before the INSERT in ``create_lead_call_tracker``, so any
future code path that forgets the id fails loudly instead of silently
creating rows that are invisible to id-filtered analytics and unloadable
at call time.
"""

from __future__ import annotations

import pytest

from app.database.accessor.breeze_buddy.lead_call_tracker import (
    require_template_link,
)
from app.schemas import IVR_OPTIONS_TEMPLATE, UNKNOWN_TEMPLATE


def test_normal_template_without_id_is_rejected():
    with pytest.raises(ValueError, match="without template_id"):
        require_template_link("cod-order-confirmation", None)


def test_empty_string_id_is_rejected():
    with pytest.raises(ValueError, match="without template_id"):
        require_template_link("cod-order-confirmation", "")


@pytest.mark.parametrize("placeholder", [IVR_OPTIONS_TEMPLATE, UNKNOWN_TEMPLATE])
def test_sanctioned_placeholders_may_omit_id(placeholder):
    require_template_link(placeholder, None)  # must not raise


def test_id_linked_lead_passes():
    require_template_link(
        "cod-order-confirmation", "6b1f0d3c-8a2e-4f5b-9c7d-1e2a3b4c5d6e"
    )
