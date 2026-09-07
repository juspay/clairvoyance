"""The node vocabulary registry (modules/05-outreach, ruled 31 Aug 2026):
the schema's Literal and NODE_TYPES are two halves of one language, pinned
together here so a type added to only one side fails CI."""

from typing import get_args

from app.crm.outreach.nodes import NODE_TYPES, is_wait
from app.crm.outreach.schemas import WorkflowNode


def _literal_words() -> set:
    return set(get_args(WorkflowNode.model_fields["type"].annotation))


def test_registry_and_schema_literal_speak_the_same_words() -> None:
    assert set(NODE_TYPES) == _literal_words()


def test_a_wait_has_no_action_and_an_action_is_not_a_wait() -> None:
    # is_wait and execute are two views of one fact: landing on a wait IS
    # the action (the alarm); every other type must do something.
    for word, spec in NODE_TYPES.items():
        assert spec.is_wait == (spec.execute is None), word
        assert callable(spec.validate), word


def test_is_wait_answers_for_every_word() -> None:
    answers = {
        word: is_wait(WorkflowNode(id="n", type=word))  # type: ignore[arg-type]
        for word in NODE_TYPES
    }
    assert answers == {
        "wait": True,
        "wait_event": True,
        "send": False,
        "call": False,
        "action": False,
    }


def test_the_package_init_exports_the_registry_and_nothing_else() -> None:
    """The one sanctioned non-empty __init__ (record/extractors precedent)
    assembles the registry; it does not re-export its siblings. The split
    of 7 Sep 2026 shipped a 21-name hub (three of them private) so twelve
    importers could stay unchanged — the accessor/__init__ scar in a new
    coat. Importers name the file they mean: nodes.context, nodes.spec,
    nodes.wait_event, nodes.<word>."""
    import app.crm.outreach.nodes as package

    assert set(package.__all__) == {"NODE_TYPES", "NodeSpec", "is_wait"}
    exported = {
        name
        for name in dir(package)
        if not name.startswith("__")
        and not name.startswith("_")
        and name not in package.__all__
        # the word modules and the siblings are attributes of any package
        # once imported; only NAMES the init defines or re-exports count
        and name
        not in {"action", "call", "send", "wait", "wait_event", "context", "spec"}
        and name not in {"Dict", "WorkflowNode"}
    }
    assert exported == set(), f"__init__ grew a re-export: {sorted(exported)}"
    assert not [n for n in dir(package) if n.startswith("_") and not n.startswith("__")]
