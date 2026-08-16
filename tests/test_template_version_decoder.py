"""Decoder tests for template version rows.

asyncpg.Record supports dict-style access; plain dicts stand in for rows,
same technique the existing decoder uses via .get()/[] access.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, cast

import asyncpg

from app.database.decoder.breeze_buddy.template_version import (
    decode_template_version,
    decode_template_version_metadata_list,
)

NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)


def _full_row(**overrides: Any) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "id": "11111111-2222-3333-4444-555555555555",
        "template_id": "6b1f0d3c-8a2e-4f5b-9c7d-1e2a3b4c5d6e",
        "version_number": 3,
        "name": "order-confirmation",
        "flow": '{"initial_node": "greeting", "nodes": {"greeting": {}}}',
        "configurations": '{"stt_language": "en"}',
        "expected_payload_schema": None,
        "expected_callback_response_schema": '{"type": "object"}',
        "updated_by": "ops@breeze.in",
        "change_source": "update",
        "restored_from": None,
        "created_at": NOW,
    }
    row.update(overrides)
    return row


def test_decode_full_version_parses_json_strings():
    version = decode_template_version(cast(asyncpg.Record, _full_row()))
    assert version is not None
    assert version.version_number == 3
    assert version.flow["initial_node"] == "greeting"
    assert version.configurations == {"stt_language": "en"}
    assert version.expected_payload_schema is None
    assert version.expected_callback_response_schema == {"type": "object"}
    assert version.change_source == "update"


def test_decode_accepts_already_parsed_jsonb_dicts():
    version = decode_template_version(
        cast(
            asyncpg.Record,
            _full_row(
                flow={"initial_node": "a", "nodes": {"a": {}}}, configurations=None
            ),
        )
    )
    assert version is not None
    assert version.flow["initial_node"] == "a"
    assert version.configurations is None


def test_decode_rollback_row_carries_restored_from():
    version = decode_template_version(
        cast(
            asyncpg.Record,
            _full_row(change_source="rollback", restored_from=5, version_number=11),
        )
    )
    assert version is not None
    assert version.change_source == "rollback"
    assert version.restored_from == 5


def test_decode_none_returns_none():
    assert decode_template_version(None) is None


def test_decode_metadata_list():
    rows = [
        {
            "id": "11111111-2222-3333-4444-555555555555",
            "template_id": "6b1f0d3c-8a2e-4f5b-9c7d-1e2a3b4c5d6e",
            "version_number": n,
            "name": "order-confirmation",
            "updated_by": None,
            "change_source": "update",
            "restored_from": None,
            "created_at": NOW,
        }
        for n in (2, 1)
    ]
    metas = decode_template_version_metadata_list(cast(List[asyncpg.Record], rows))
    assert [m.version_number for m in metas] == [2, 1]
    assert decode_template_version_metadata_list(None) == []
