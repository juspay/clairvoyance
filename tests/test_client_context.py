# pyrefly: ignore-errors
"""Tests for the client-pushed context engine
([[chat.client_context.apply_context_patch]] +
[[chat.client_context.render_client_context]]).

Both are pure functions — no DB, no I/O. We exercise: allowlist
filtering, the reserved-key guard, size caps, merge modes, the
user_tail/system placement split, and the trusted-key elevation gate.
"""

from __future__ import annotations

import json

import pytest

from app.ai.voice.agents.breeze_buddy.chat.client_context import (
    CLIENT_CONTEXT_KEY,
    CLIENT_CONTEXT_REV_KEY,
    ClientContextTooLarge,
    apply_context_patch,
    render_client_context,
)
from app.ai.voice.agents.breeze_buddy.template.types import ClientContextConfig


def _cfg(**overrides) -> ClientContextConfig:
    base = dict(
        state_allowlist=["cart_id"],
        facts_allowlist=["offers", "cart_summary"],
        max_bytes=4096,
        render=True,
        facts_placement="user_tail",
        trusted_facts=[],
    )
    base.update(overrides)
    return ClientContextConfig(**base)


# ---------------------------------------------------------------------------
# apply_context_patch
# ---------------------------------------------------------------------------


def test_no_config_is_inert():
    state, sk, fk = apply_context_patch(
        {"cart_id": "keep"},
        state={"cart_id": "new"},
        facts={"offers": []},
        merge="shallow",
        config=None,
    )
    assert state == {"cart_id": "keep"}
    assert sk == [] and fk == []


def test_state_allowlist_filters_unknown_keys():
    state, sk, fk = apply_context_patch(
        {},
        state={"cart_id": "gid://Cart/1", "is_admin": True},
        facts=None,
        merge="shallow",
        config=_cfg(),
    )
    assert state["cart_id"] == "gid://Cart/1"
    assert "is_admin" not in state
    assert sk == ["cart_id"] and fk == []


def test_reserved_keys_cannot_be_set_via_state():
    state, sk, _ = apply_context_patch(
        {},
        state={CLIENT_CONTEXT_KEY: {"evil": 1}, CLIENT_CONTEXT_REV_KEY: 99},
        facts=None,
        merge="shallow",
        config=_cfg(state_allowlist=[CLIENT_CONTEXT_KEY, CLIENT_CONTEXT_REV_KEY]),
    )
    # Even if a misconfigured allowlist names them, the reserved guard drops them.
    assert CLIENT_CONTEXT_KEY not in state
    assert CLIENT_CONTEXT_REV_KEY not in state
    assert sk == []


def test_facts_merge_into_namespace_and_allowlist():
    state, _, fk = apply_context_patch(
        {},
        state=None,
        facts={"offers": [{"code": "X"}], "secret": "drop"},
        merge="shallow",
        config=_cfg(),
    )
    assert state[CLIENT_CONTEXT_KEY] == {"offers": [{"code": "X"}]}
    assert "secret" not in state[CLIENT_CONTEXT_KEY]
    assert fk == ["offers"]


def test_facts_shallow_merge_preserves_prior():
    prior = {CLIENT_CONTEXT_KEY: {"offers": [1], "cart_summary": {"n": 1}}}
    state, _, _ = apply_context_patch(
        prior,
        state=None,
        facts={"offers": [2]},
        merge="shallow",
        config=_cfg(),
    )
    assert state[CLIENT_CONTEXT_KEY] == {"offers": [2], "cart_summary": {"n": 1}}


def test_facts_replace_clears_namespace():
    prior = {CLIENT_CONTEXT_KEY: {"offers": [1], "cart_summary": {"n": 1}}}
    state, _, _ = apply_context_patch(
        prior,
        state=None,
        facts={"offers": [2]},
        merge="replace",
        config=_cfg(),
    )
    assert state[CLIENT_CONTEXT_KEY] == {"offers": [2]}


def test_state_replace_clears_only_allowlisted_keys():
    prior = {"cart_id": "old", "checkout_id": "keep"}
    state, _, _ = apply_context_patch(
        prior,
        state={},
        facts=None,
        merge="replace",
        config=_cfg(state_allowlist=["cart_id"]),
    )
    # cart_id cleared (allowlisted, not re-supplied); checkout_id untouched.
    assert "cart_id" not in state
    assert state["checkout_id"] == "keep"


def test_size_cap_raises():
    big = {"offers": "x" * 5000}
    with pytest.raises(ClientContextTooLarge):
        apply_context_patch(
            {},
            state=None,
            facts=big,
            merge="shallow",
            config=_cfg(facts_allowlist=["offers"], max_bytes=1024),
        )


def test_input_state_not_mutated():
    original = {"cart_id": "a"}
    apply_context_patch(
        original,
        state={"cart_id": "b"},
        facts=None,
        merge="shallow",
        config=_cfg(),
    )
    assert original == {"cart_id": "a"}


# ---------------------------------------------------------------------------
# render_client_context
# ---------------------------------------------------------------------------


def test_render_disabled_returns_none():
    state = {CLIENT_CONTEXT_KEY: {"offers": [1]}}
    assert render_client_context(state, _cfg(render=False)) == (None, None)


def test_render_no_facts_returns_none():
    assert render_client_context({}, _cfg()) == (None, None)


def test_render_user_tail_default():
    state = {CLIENT_CONTEXT_KEY: {"offers": [{"code": "X"}]}}
    user_block, system_block = render_client_context(state, _cfg())
    assert system_block is None
    assert user_block is not None
    assert (
        "[storefront_context]" in user_block and "[/storefront_context]" in user_block
    )
    assert '"offers"' in user_block


def test_render_system_only_elevates_trusted():
    state = {CLIENT_CONTEXT_KEY: {"promo_policy": "10% off", "cart_summary": {"n": 3}}}
    cfg = _cfg(
        facts_allowlist=["promo_policy", "cart_summary"],
        facts_placement="system",
        trusted_facts=["promo_policy"],
    )
    user_block, system_block = render_client_context(state, cfg)
    assert system_block is not None and "promo_policy" in system_block
    # cart_summary is NOT trusted → stays user_tail even though placement=system.
    assert user_block is not None and "cart_summary" in user_block
    assert "cart_summary" not in system_block
    assert "promo_policy" not in user_block


def test_render_placement_override_bounded_by_trusted():
    state = {CLIENT_CONTEXT_KEY: {"cart_summary": {"n": 3}}}
    cfg = _cfg(facts_allowlist=["cart_summary"], trusted_facts=[])
    # Per-push asks for system, but cart_summary isn't trusted → user_tail only.
    user_block, system_block = render_client_context(
        state, cfg, placement_override="system"
    )
    assert system_block is None
    assert user_block is not None and "cart_summary" in user_block


def test_render_payload_is_valid_json_inside_delimiters():
    state = {CLIENT_CONTEXT_KEY: {"offers": [{"code": "WELCOME10"}]}}
    user_block, _ = render_client_context(state, _cfg())
    assert user_block is not None
    body = user_block.splitlines()[1]
    assert json.loads(body) == {"offers": [{"code": "WELCOME10"}]}
