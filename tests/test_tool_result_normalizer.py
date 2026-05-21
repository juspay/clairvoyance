"""Tests for app.ai.voice.agents.breeze_buddy.chat.tool_result_normalizer.

Self-contained: stdlib + the normalizer module only.  Run with::

    uv run pytest tests/test_tool_result_normalizer.py -v

The module's job is now solely to peel the Clairvoyance + pipecat MCP
envelope; per-tool shaping (numeric scaling, field flattening, image
precedence) lives in the template's ``tool_response_transforms``.
"""

import json

from app.ai.voice.agents.breeze_buddy.chat.tool_result_normalizer import normalize


def _wrap(inner) -> dict:
    """Clairvoyance + pipecat MCP envelope."""
    return {"status": "success", "data": json.dumps(inner)}


class TestEnvelopeUnwrap:
    def test_unwraps_success_envelope_to_inner_dict(self):
        inner = {"products": [{"id": "gid://shopify/Product/1", "title": "Board"}]}
        assert normalize("search_catalog", _wrap(inner)) == inner

    def test_inner_payload_passes_through_untouched(self):
        # No per-tool flattening — raw MCP shape is preserved verbatim so the
        # LLM can reference fields like ``data.products[0].price_range.min.amount``.
        inner = {
            "products": [
                {
                    "id": "gid://shopify/Product/1",
                    "title": "Snowboard",
                    "price_range": {"min": {"amount": 69995, "currency": "INR"}},
                    "media": [
                        {"type": "image", "url": "https://cdn.example.com/x.jpg"}
                    ],
                }
            ]
        }
        out = normalize("search_catalog", _wrap(inner))
        assert out == inner
        # Raw minor-unit price is preserved (no server-side scaling).
        assert out["products"][0]["price_range"]["min"]["amount"] == 69995

    def test_error_status_returns_raw_envelope(self):
        payload = {"status": "error", "data": json.dumps({"products": []})}
        # Cannot unwrap an error envelope → fall back to the raw payload so
        # the LLM still sees *something* and the turn doesn't blow up.
        assert normalize("search_catalog", payload) == payload

    def test_malformed_json_in_data_returns_raw_envelope(self):
        payload = {"status": "success", "data": "this is not json {"}
        assert normalize("search_catalog", payload) == payload

    def test_unenveloped_dict_passes_through(self):
        raw = {"products": [{"id": "1", "title": "x"}]}
        assert normalize("search_catalog", raw) == raw

    def test_non_dict_input_returned_as_is(self):
        assert normalize("search_catalog", "not a dict") == "not a dict"
        assert normalize("search_catalog", None) is None
        assert normalize("search_catalog", 42) == 42

    def test_tool_name_is_ignored(self):
        # The signature keeps ``tool_name`` for forward-compat with future
        # per-tool hooks, but no current behaviour depends on it.
        payload = _wrap({"cart": {"id": "gid://shopify/Cart/1"}})
        assert normalize("get_cart", payload) == normalize("anything_else", payload)
        assert normalize("", payload) == {"cart": {"id": "gid://shopify/Cart/1"}}

    def test_nested_envelope_data_already_dict(self):
        # Some callers may bypass the pipecat stringification layer.
        inner = {"cart": {"id": "gid://shopify/Cart/9", "lines": []}}
        payload = {"status": "success", "data": inner}
        assert normalize("get_cart", payload) == inner
