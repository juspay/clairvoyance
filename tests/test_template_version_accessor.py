"""Snapshot serialization contract for the template-write transaction."""

from app.database.accessor.breeze_buddy.template import (
    serialize_version_snapshot,
)


def test_serialize_version_snapshot_dumps_present_blobs():
    flow_json, config_json, payload_json, callback_json = serialize_version_snapshot(
        flow={"initial_node": "a", "nodes": {"a": {}}},
        snapshot_configurations={"stt_language": "en"},
        expected_payload_schema={"type": "object"},
        expected_callback_response_schema=None,
    )
    assert '"initial_node"' in flow_json
    assert config_json == '{"stt_language": "en"}'
    assert payload_json == '{"type": "object"}'
    assert callback_json is None


def test_serialize_version_snapshot_none_stays_none_not_json_null():
    _, config_json, payload_json, callback_json = serialize_version_snapshot(
        flow={},
        snapshot_configurations=None,
        expected_payload_schema=None,
        expected_callback_response_schema=None,
    )
    assert config_json is None
    assert payload_json is None
    assert callback_json is None
