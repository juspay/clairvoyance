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

import asyncpg
import pytest
from loguru import logger

import app.database.accessor.breeze_buddy.lead_call_tracker as acc
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


# --- duplicate insert (walker lease retry) -----------------------------------
#
# The CRM walker mints a deterministic uuid5 lead id per (run, node, visit),
# so a lease retry re-inserts the same id and the primary key refuses it.
# call.py already treats None as "maybe it exists", looks the row up and
# adopts it — prod on 2026-10-12 showed 54/54 adopted, 0 real failures — so
# the accessor must not log that as an ERROR. It is a WARNING naming the id;
# the return stays None so no caller changes.


@pytest.fixture
def log_lines():
    """(level, message) pairs logged at WARNING+ (loguru, so not caplog)."""
    lines: list[tuple[str, str]] = []
    sink = logger.add(
        lambda m: lines.append((m.record["level"].name, m.record["message"])),
        level="WARNING",
    )
    yield lines
    logger.remove(sink)


async def test_duplicate_lead_insert_is_a_warning_not_an_error(monkeypatch, log_lines):
    async def duplicate(text, values):
        raise asyncpg.UniqueViolationError(
            'duplicate key value violates unique constraint "lead_call_tracker_pkey"'
        )

    monkeypatch.setattr(acc, "run_parameterized_query", duplicate)

    result = await acc.create_lead_call_tracker(
        id="lead-dup-1",
        reseller_id="r1",
        template="cod-order-confirmation",
        template_id="6b1f0d3c-8a2e-4f5b-9c7d-1e2a3b4c5d6e",
        merchant_id="m1",
        next_attempt_at=None,
        payload={},
    )

    assert result is None
    assert [lvl for lvl, _ in log_lines] == ["WARNING"]
    assert "lead-dup-1" in log_lines[0][1]
    assert "already exists" in log_lines[0][1]


async def test_other_insert_failures_still_log_error(monkeypatch, log_lines):
    async def boom(text, values):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(acc, "run_parameterized_query", boom)

    result = await acc.create_lead_call_tracker(
        id="lead-err-1",
        reseller_id="r1",
        template="cod-order-confirmation",
        template_id="6b1f0d3c-8a2e-4f5b-9c7d-1e2a3b4c5d6e",
        merchant_id="m1",
        next_attempt_at=None,
        payload={},
    )

    assert result is None
    assert [lvl for lvl, _ in log_lines] == ["ERROR"]
