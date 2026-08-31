"""W1 publish-validator laws: the exact edit classes canon T19 says the
validator must block, each as a red test."""

from app.crm.outreach.plans import validate_definition


def _definition(**overrides):
    base = {
        "entry": {"topic": "checkout.initiated", "reenter": True, "cooldown_hours": 0},
        "nodes": [
            {"id": "wait-30m", "type": "wait", "minutes": 30},
            {"id": "rescue-call", "type": "call", "template_id": "tpl-1"},
        ],
        "edges": [["wait-30m", "rescue-call"]],
        "goal": {"topics": ["order.placed"]},
    }
    base.update(overrides)
    return base


def test_valid_definition_passes() -> None:
    assert validate_definition(_definition()) == []


def test_duplicate_node_ids_fail() -> None:
    problems = validate_definition(
        _definition(
            nodes=[
                {"id": "a", "type": "wait", "minutes": 5},
                {"id": "a", "type": "call", "template_id": "t"},
            ],
            edges=[],
        )
    )
    assert any("duplicate node id" in p for p in problems)


def test_edge_to_unknown_node_fails() -> None:
    problems = validate_definition(_definition(edges=[["wait-30m", "ghost"]]))
    assert any("unknown node: ghost" in p for p in problems)


def test_two_plain_edges_out_of_one_node_fail() -> None:
    problems = validate_definition(
        _definition(
            nodes=[
                {"id": "a", "type": "wait", "minutes": 1},
                {"id": "b", "type": "wait", "minutes": 1},
                {"id": "c", "type": "wait", "minutes": 1},
            ],
            edges=[["a", "b"], ["a", "c"]],
        )
    )
    assert any("2 outgoing edges" in p for p in problems)


def test_wait_without_minutes_fails() -> None:
    problems = validate_definition(
        _definition(nodes=[{"id": "w", "type": "wait"}], edges=[])
    )
    assert any("needs minutes" in p for p in problems)


def test_call_without_template_id_fails() -> None:
    problems = validate_definition(
        _definition(nodes=[{"id": "c", "type": "call"}], edges=[])
    )
    assert any("needs a template_id" in p for p in problems)


def test_send_without_template_fails() -> None:
    problems = validate_definition(
        _definition(nodes=[{"id": "s", "type": "send"}], edges=[])
    )
    assert any("needs a template" in p for p in problems)


def test_unknown_node_type_fails_shape() -> None:
    problems = validate_definition(
        _definition(nodes=[{"id": "x", "type": "teleport"}], edges=[])
    )
    assert any("shape invalid" in p for p in problems)


def test_exit_ceiling_must_be_positive() -> None:
    for bad in (0, -1):
        problems = validate_definition(_definition(exits={"max_age_days": bad}))
        assert problems and "shape invalid" in problems[0]
    assert validate_definition(_definition(exits={"max_age_days": 0.5})) == []


def test_cooldown_cannot_be_negative() -> None:
    entry = {"topic": "checkout.initiated", "reenter": True, "cooldown_hours": -1}
    problems = validate_definition(_definition(entry=entry))
    assert problems and "shape invalid" in problems[0]


def test_occupied_node_deletion_fails() -> None:
    """The stranding law: a document that removes a square waiting tokens
    stand on must not publish."""
    problems = validate_definition(_definition(), occupied_nodes=["old-node"])
    assert any("waiting runs standing on it" in p for p in problems)


def test_occupied_node_kept_passes() -> None:
    assert validate_definition(_definition(), occupied_nodes=["wait-30m"]) == []


def test_publish_refuses_an_entry_change_while_runs_are_open() -> None:
    draft = {
        "entry": {"topic": "cart.abandoned"},
        "nodes": [{"id": "w", "type": "wait", "minutes": 30}],
        "edges": [],
        "goal": {"topics": ["order.placed"]},
    }
    live_entry = {"topic": "checkout.initiated"}
    assert validate_definition(draft, occupied_nodes=["w"], live_entry=live_entry)
    assert not validate_definition(draft, occupied_nodes=[], live_entry=live_entry)
    assert not validate_definition(draft, occupied_nodes=["w"], live_entry=None)


_COD = {
    "entry": {"topic": "orders/create", "where": {"gateway": "COD"}},
    "nodes": [
        {
            "id": "ask",
            "type": "wait_event",
            "topics": ["button.reply"],
            "key": "button_id",
            "minutes": 60,
        },
        {"id": "confirm", "type": "wait", "minutes": 1},
        {"id": "call", "type": "wait", "minutes": 1},
    ],
    "edges": [["ask", "confirm", "YES"], ["ask", "call", "timeout"]],
    "goal": {"topics": ["order.confirmed"]},
}


def test_wait_event_with_labelled_edges_passes() -> None:
    assert validate_definition(_COD) == []


def test_wait_event_needs_topics_key_and_minutes() -> None:
    bad = {**_COD, "nodes": [{"id": "ask", "type": "wait_event"}, *_COD["nodes"][1:]]}
    problems = validate_definition(bad)
    assert any("needs minutes" in p for p in problems)
    assert any("needs topics" in p for p in problems)
    assert any("needs a payload key" in p for p in problems)


def test_edges_out_of_wait_event_must_be_labelled_and_distinct() -> None:
    unlabelled = {**_COD, "edges": [["ask", "confirm"]]}
    assert any("needs an on" in p for p in validate_definition(unlabelled))
    twice = {**_COD, "edges": [["ask", "confirm", "YES"], ["ask", "call", "YES"]]}
    assert any("same on" in p for p in validate_definition(twice))


def test_only_wait_event_may_label_edges() -> None:
    bad = {**_COD, "edges": [["ask", "confirm", "YES"], ["confirm", "call", "YES"]]}
    assert any("only a wait_event" in p for p in validate_definition(bad))


def test_send_node_needs_channel_and_a_plan_purpose() -> None:
    bare = _definition(
        nodes=[{"id": "ask", "type": "send", "template": "cod_confirm"}], edges=[]
    )
    problems = validate_definition(bare)
    assert any("needs a channel" in p for p in problems)
    assert any("purpose_key" in p for p in problems)
    full = _definition(
        nodes=[
            {
                "id": "ask",
                "type": "send",
                "template": "cod_confirm",
                "channel": "whatsapp",
            }
        ],
        edges=[],
        purpose_key="utility.order.cod_confirm",
    )
    assert validate_definition(full) == []


def test_entry_key_is_document_vocabulary() -> None:
    keyed = {
        "topic": "checkout.initiated",
        "reenter": True,
        "cooldown_hours": 0,
        "key": "order_id",
    }
    assert validate_definition(_definition(entry=keyed)) == []
    empty = {**keyed, "key": ""}
    problems = validate_definition(_definition(entry=empty))
    assert problems and "shape invalid" in problems[0]


def test_changing_entry_key_mid_flight_is_blocked() -> None:
    live_entry = {"topic": "checkout.initiated", "reenter": True, "cooldown_hours": 0}
    keyed = {**live_entry, "key": "order_id"}
    problems = validate_definition(
        _definition(entry=keyed), occupied_nodes=["wait-30m"], live_entry=live_entry
    )
    assert any("entry rule changed" in p for p in problems)
