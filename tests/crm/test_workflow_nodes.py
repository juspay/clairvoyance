"""The node vocabulary registry (modules/05-outreach, ruled 31 Aug 2026):
the schema's Literal and NODE_TYPES are two halves of one language, pinned
together here so a type added to only one side fails CI. Since 17 Sep 2026
one word, `wait`, has three forms (a timer, a listening timer, either held
to hours); `wait_event` is retired but readable."""

from typing import get_args

from app.crm.outreach.nodes import NODE_TYPES, branches, is_wait, listens
from app.crm.outreach.schemas import RETIRED_WAIT_EVENT, WorkflowNode


def _literal_words() -> set:
    return set(get_args(WorkflowNode.model_fields["type"].annotation))


def test_registry_and_schema_literal_speak_the_same_words() -> None:
    """The Literal keeps the retired word so stored rows still parse; the
    registry never answers for it (the model reads it as a wait first)."""
    assert set(NODE_TYPES) == _literal_words() - {RETIRED_WAIT_EVENT}
    assert RETIRED_WAIT_EVENT in _literal_words()


def test_only_condition_and_split_branch_by_word() -> None:
    """N1 retired (enh A/01): nothing matches a type string. A condition or
    split always labels its edges; a wait's labels depend on its topics."""
    assert {w for w, s in NODE_TYPES.items() if s.branches} == {"condition", "split"}


def test_a_wait_listens_and_branches_exactly_when_it_lists_topics() -> None:
    timer = WorkflowNode(id="t", type="wait", minutes=15)
    hearing = WorkflowNode(id="h", type="wait", minutes=15, topics=["x"], key="$topic")
    assert (listens(timer), branches(timer)) == (False, False)
    assert (listens(hearing), branches(hearing)) == (True, True)
    # topics on a word that cannot wait make it neither
    assert listens(WorkflowNode(id="c", type="call", topics=["x"])) is False


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
        "send": False,
        "call": False,
        "action": False,
        "condition": False,
        "split": False,
    }


def test_the_package_init_exports_the_registry_and_nothing_else() -> None:
    """The one sanctioned non-empty __init__ (record/extractors precedent)
    assembles the registry and the three questions asked of a node; it does
    not re-export its siblings. The split of 7 Sep 2026 shipped a 21-name
    hub (three of them private) so twelve importers could stay unchanged —
    the accessor/__init__ scar in a new coat. Importers name the file they
    mean: nodes.context, nodes.spec, nodes.wait, nodes.<word>."""
    import app.crm.outreach.nodes as package

    assert set(package.__all__) == {
        "NODE_TYPES",
        "NodeSpec",
        "branches",
        "is_wait",
        "listens",
    }
    exported = {
        name
        for name in dir(package)
        if not name.startswith("__")
        and not name.startswith("_")
        and name not in package.__all__
        # the word modules and the siblings are attributes of any package
        # once imported; only NAMES the init defines or re-exports count
        and name
        not in {
            "action",
            "call",
            "condition",
            "send",
            "split",
            "wait",
            "context",
            "spec",
        }
        and name not in {"Dict", "WorkflowNode"}
    }
    assert exported == set(), f"__init__ grew a re-export: {sorted(exported)}"
    assert not [n for n in dir(package) if n.startswith("_") and not n.startswith("__")]
