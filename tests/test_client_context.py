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
    compute_context_patch,
    diff_state_patch,
    merge_context_into,
    render_client_context,
    strip_client_context_keys,
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


# ---------------------------------------------------------------------------
# strip_client_context_keys — the turn writers exclude these so a merge
# write never clobbers a concurrent /context push.
# ---------------------------------------------------------------------------


def test_strip_excludes_client_context_keys():
    data = {
        "cart_id": "c1",
        CLIENT_CONTEXT_KEY: {"offers": []},
        CLIENT_CONTEXT_REV_KEY: 5,
    }
    assert strip_client_context_keys(data) == {"cart_id": "c1"}


def test_strip_is_passthrough_without_client_context():
    assert strip_client_context_keys({"cart_id": "c1"}) == {"cart_id": "c1"}
    assert strip_client_context_keys({}) == {}


# ---------------------------------------------------------------------------
# diff_state_patch — the turn writer persists ONLY the keys its reducers
# changed vs the baseline loaded at turn start, so a top-level merge can't
# re-assert a stale allowlisted key and clobber a concurrent /context push.
# ---------------------------------------------------------------------------


def test_diff_state_patch_only_changed_keys():
    baseline = {"cart_id": "c1", "checkout_id": "k1"}
    current = {"cart_id": "c2", "checkout_id": "k1"}  # only cart_id changed
    assert diff_state_patch(baseline, current) == {"cart_id": "c2"}


def test_diff_state_patch_includes_new_keys():
    assert diff_state_patch({}, {"cart_id": "c1"}) == {"cart_id": "c1"}


def test_diff_state_patch_empty_when_unchanged():
    state = {"cart_id": "c1", "checkout_id": "k1"}
    assert diff_state_patch(state, state) == {}


def test_diff_state_patch_excludes_client_context_keys():
    # Even when the live state carries the /context-owned keys (loaded off the
    # row), the turn's patch must never include them.
    baseline = {"cart_id": "c1"}
    current = {
        "cart_id": "c1",
        CLIENT_CONTEXT_KEY: {"offers": ["x"]},
        CLIENT_CONTEXT_REV_KEY: 9,
    }
    assert diff_state_patch(baseline, current) == {}


def test_diff_state_patch_does_not_reassert_untouched_key():
    # The turn loaded cart_id=c1 but the reducers only added order_id; the
    # patch must omit cart_id so a concurrent /context push of it isn't lost.
    baseline = {"cart_id": "c1"}
    current = {"cart_id": "c1", "order_id": "o1"}
    assert diff_state_patch(baseline, current) == {"order_id": "o1"}


# ---------------------------------------------------------------------------
# compute_context_patch — returns the FILTERED patches (no merge); the merge
# happens atomically in Postgres. Mirrors apply_context_patch's filtering.
# ---------------------------------------------------------------------------


def test_compute_patch_filters_to_allowlist():
    sp, fp, replace, sk, fk = compute_context_patch(
        {},
        state={"cart_id": "c1", "is_admin": True},
        facts={"offers": ["a"], "junk": 1},
        merge="shallow",
        config=_cfg(),
    )
    assert sp == {"cart_id": "c1"}
    assert fp == {"offers": ["a"]}
    assert replace is False
    assert sk == ["cart_id"] and fk == ["offers"]


def test_compute_patch_no_config_is_inert():
    assert compute_context_patch(
        {}, state={"cart_id": "c"}, facts={"offers": []}, merge="shallow", config=None
    ) == ({}, None, False, [], [])


def test_compute_patch_no_facts_returns_none_facts_patch():
    # No ``facts`` in the push => facts_patch is None => the SQL leaves the
    # _client_context namespace untouched (so a state-only replace can't wipe
    # facts). This is the load-bearing distinction from an empty {} facts.
    _, fp, _, _, fk = compute_context_patch(
        {}, state={"cart_id": "c"}, facts=None, merge="shallow", config=_cfg()
    )
    assert fp is None and fk == []


def test_compute_patch_replace_nulls_cleared_state_keys():
    sp, fp, replace, _, _ = compute_context_patch(
        {"cart_id": "old"}, state={}, facts=None, merge="replace", config=_cfg()
    )
    # Allowlisted key absent from this push is nulled so the SQL ``||`` merge
    # clears it (merge can overwrite but not delete).
    assert sp == {"cart_id": None}
    assert replace is True
    # State-only replace must NOT touch facts (regression guard).
    assert fp is None


def test_compute_patch_size_guard_uses_existing_facts():
    cfg = _cfg(max_bytes=30)
    with pytest.raises(ClientContextTooLarge):
        compute_context_patch(
            {CLIENT_CONTEXT_KEY: {"offers": "x" * 40}},
            state=None,
            facts={"cart_summary": "y" * 40},
            merge="shallow",
            config=cfg,
        )


def test_compute_patch_max_bytes_zero_is_a_real_cap():
    # max_bytes=0 must REJECT non-empty facts (not be treated as "disabled").
    cfg = _cfg(max_bytes=0)
    with pytest.raises(ClientContextTooLarge):
        compute_context_patch(
            {}, state=None, facts={"offers": ["x"]}, merge="shallow", config=cfg
        )
    # ...but an empty facts namespace (a clear) is still allowed at 0.
    _, fp, _, _, _ = compute_context_patch(
        {}, state=None, facts={}, merge="replace", config=cfg
    )
    assert fp == {}


# ---------------------------------------------------------------------------
# merge_context_into — pure in-memory mirror of the SQL merge (used to update
# a turn's in-flight agent_state so THIS turn sees in-message context).
# ---------------------------------------------------------------------------


def test_merge_into_overlays_state_and_deep_merges_facts():
    base = {"cart_id": "c1", CLIENT_CONTEXT_KEY: {"offers": ["a"]}}
    out = merge_context_into(base, {"cart_id": "c2"}, {"cart_summary": "s"}, False)
    assert out["cart_id"] == "c2"
    assert out[CLIENT_CONTEXT_KEY] == {"offers": ["a"], "cart_summary": "s"}


def test_merge_into_replace_overwrites_facts_namespace():
    base = {CLIENT_CONTEXT_KEY: {"offers": ["a"]}}
    out = merge_context_into(base, {}, {"cart_summary": "s"}, True)
    assert out[CLIENT_CONTEXT_KEY] == {"cart_summary": "s"}


def test_merge_into_strips_nulled_state_keys():
    out = merge_context_into({"cart_id": "old"}, {"cart_id": None}, None, False)
    assert "cart_id" not in out


def test_merge_into_none_facts_leaves_namespace_untouched():
    # State-only replace (facts_patch=None) must preserve existing facts.
    base = {"cart_id": "old", CLIENT_CONTEXT_KEY: {"offers": ["keep"]}}
    out = merge_context_into(base, {"cart_id": None}, None, True)
    assert "cart_id" not in out
    assert out[CLIENT_CONTEXT_KEY] == {"offers": ["keep"]}  # NOT wiped


def test_apply_context_patch_equals_compute_plus_merge():
    # The wrapper must equal compute + merge (single source of truth).
    state_data = {"cart_id": "old", CLIENT_CONTEXT_KEY: {"offers": ["a"]}}
    cfg = _cfg()
    via_wrapper, sk, fk = apply_context_patch(
        state_data,
        state={"cart_id": "new"},
        facts={"cart_summary": "s"},
        merge="shallow",
        config=cfg,
    )
    sp, fp, replace, sk2, fk2 = compute_context_patch(
        state_data,
        state={"cart_id": "new"},
        facts={"cart_summary": "s"},
        merge="shallow",
        config=cfg,
    )
    via_parts = merge_context_into(state_data, sp, fp, replace)
    assert via_wrapper == via_parts
    assert (sk, fk) == (sk2, fk2)


# ---------------------------------------------------------------------------
# The decouple invariant: a turn write and a /context write touch DISJOINT
# owned keys, so a top-level merge of either never clobbers the other.
# ---------------------------------------------------------------------------


def test_turn_patch_and_context_keys_are_disjoint():
    # Turn loaded a row that already carries /context-owned keys; its merge
    # patch must drop them entirely (so the DB merge leaves them untouched).
    turn_state = {
        "cart_id": "c1",
        CLIENT_CONTEXT_KEY: {"offers": ["stale"]},
        CLIENT_CONTEXT_REV_KEY: 3,
    }
    turn_patch = strip_client_context_keys(turn_state)
    assert CLIENT_CONTEXT_KEY not in turn_patch
    assert CLIENT_CONTEXT_REV_KEY not in turn_patch
    assert turn_patch == {"cart_id": "c1"}


# ---------------------------------------------------------------------------
# Atomic SQL shape — the merge query must deep-merge facts + guard revision.
# (Behaviour is DB-level; this guards against accidental SQL regressions.)
# ---------------------------------------------------------------------------


def test_merge_client_context_query_shape():
    from app.database.queries.breeze_buddy.chat_session import (
        merge_client_context_query,
    )

    sql, params = merge_client_context_query(
        "sess-1", '{"a":1}', '{"offers":[]}', 7, False, True
    )
    # touch_facts ($6) gates the jsonb_set deep-merge of the namespace...
    assert "CASE WHEN $6" in sql
    assert "jsonb_set" in sql
    assert "_client_context" in sql
    # ...and applies the monotonic revision guard.
    assert "_client_context_rev" in sql
    assert "< $4::bigint" in sql
    assert params == ["sess-1", '{"a":1}', '{"offers":[]}', 7, False, True]


def test_merge_client_context_query_skips_facts_when_not_touched():
    from app.database.queries.breeze_buddy.chat_session import (
        merge_client_context_query,
    )

    # touch_facts=False => the ELSE branch is a plain top-level merge; the
    # facts namespace is never recomputed (state-only / revision-only push).
    _, params = merge_client_context_query("s", "{}", "{}", None, False, False)
    assert params[-1] is False


def test_upsert_merge_query_is_shallow_top_level_merge():
    from app.database.queries.breeze_buddy.chat_session import (
        upsert_agent_session_state_merge_query,
    )

    sql, params = upsert_agent_session_state_merge_query("sess-1", '{"cart_id":"c"}')
    assert ".data || EXCLUDED.data" in sql
    assert "jsonb_set" not in sql  # turn writes never touch the facts namespace
    assert params == ["sess-1", '{"cart_id":"c"}']
