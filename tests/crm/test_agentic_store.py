"""The agentic attempts store: pure list rules that replaced the SQL
WHERE clauses of the old crm_customer_agent table."""

from app.crm.preview.uap.db import accessor as store


def _entry(ref, **over):
    base = {
        "agent_obj_ref": ref,
        "status": "ACTIVE",
        "action_status": "ACTIVE",
        "agent_id": "cont_a",
        "action_id": "cont_b",
        "payer_avpa": "x@upi",
        "preferred": False,
        "created_at": "2026-09-01T00:00:00+00:00",
    }
    base.update(over)
    return base


def test_upsert_is_idempotent_per_ref() -> None:
    entries, first = store.upsert_attempt(
        [],
        juspay_customer_id="cth_1",
        agent_obj_ref="agent_x_1",
        action_obj_ref="intent_x_1",
        intent_constraints={"max_per_draw": "200"},
    )
    assert first["status"] == "PENDING" and first["intent_constraints"] == {
        "max_per_draw": "200"
    }
    entries, again = store.upsert_attempt(
        entries,
        juspay_customer_id="cth_1",
        agent_obj_ref="agent_x_1",
        action_obj_ref="intent_x_1",
        intent_constraints={"max_per_draw": "999"},
    )
    assert len(entries) == 1 and again["intent_constraints"] == {"max_per_draw": "200"}


def test_multiple_agents_per_customer_and_drawable_order() -> None:
    entries = [
        _entry("a", created_at="2026-09-01T00:00:00+00:00"),
        _entry("b", created_at="2026-09-03T00:00:00+00:00"),
        _entry("c", created_at="2026-09-02T00:00:00+00:00", preferred=True),
        _entry("half", created_at="2026-09-04T00:00:00+00:00", payer_avpa=None),
        _entry("pending", created_at="2026-09-05T00:00:00+00:00", status="PENDING"),
    ]
    assert [e["agent_obj_ref"] for e in store.drawable_entries(entries)] == [
        "c",
        "b",
        "a",
    ]
    half = store.find_by_ref(entries, "half")
    assert half is not None
    assert half["payer_avpa"] is None


def test_patch_updates_only_that_attempt_and_clears_named_fields() -> None:
    entries = [_entry("a", action_id="old"), _entry("b", action_id="keep")]
    entries, patched = store.apply_patch(
        entries,
        "a",
        {"status": "PAUSED", "bogus": "ignored", "agent_id": None},
        clear=("action_id",),
    )
    assert patched is not None
    assert patched["status"] == "PAUSED" and patched["action_id"] is None
    assert patched["agent_id"] == "cont_a" and "bogus" not in patched
    b = store.find_by_ref(entries, "b")
    assert b is not None
    assert b["action_id"] == "keep"
    assert store.apply_patch(entries, "nope", {"status": "ACTIVE"})[1] is None


def test_preferred_is_exclusive() -> None:
    entries = [_entry("a", preferred=True), _entry("b"), _entry("c")]
    entries, ok = store.choose_preferred(entries, "b")
    assert ok and [e["preferred"] for e in entries] == [False, True, False]
    assert store.choose_preferred(entries, "zzz")[1] is False


def test_preferred_refuses_a_non_drawable_attempt() -> None:
    entries = [
        _entry("live", preferred=True),
        _entry("pending", status="PENDING"),
        _entry("half", payer_avpa=None),
    ]
    for ref in ("pending", "half"):
        entries, ok = store.choose_preferred(entries, ref)
        assert ok is False
    # the refusal changed nothing: the live agent is still the preferred one
    assert [e["preferred"] for e in entries] == [True, False, False]


def test_prune_keeps_live_and_newest_settled() -> None:
    settled = [
        _entry(
            f"e{i}", status="EXPIRED", created_at=f"2026-08-{i + 1:02d}T00:00:00+00:00"
        )
        for i in range(25)
    ]
    live = [_entry("live", status="PENDING", created_at="2026-07-01T00:00:00+00:00")]
    kept = store.prune(live + settled)
    refs = {e["agent_obj_ref"] for e in kept}
    assert "live" in refs and len(kept) == 1 + store.KEEP_SETTLED
    assert "e0" not in refs and "e24" in refs


def test_customer_id_from_ref() -> None:
    cid = "0dd139fc-3569-4c78-b686-e42f0e6f61fa"
    assert store.customer_id_from_ref(f"agent_{cid}_1788718478") == cid
    assert store.customer_id_from_ref("agent_not-a-uuid_1") is None
    assert store.customer_id_from_ref("OB-abc") is None
