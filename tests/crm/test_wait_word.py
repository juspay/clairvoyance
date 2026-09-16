"""One `wait` (ruled 17 Sep 2026): `wait_event` folded in, `minutes`
optional. Old stored documents must keep reading and walking; a new
document speaks the one word; a wait with no minutes takes its alarm from
the window or from the run's life."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from app.crm.outreach.catalog_laws import unenumerable_squares
from app.crm.outreach.ladder import expand_stages
from app.crm.outreach.nodes import branches, listens
from app.crm.outreach.plans import validate_definition
from app.crm.outreach.schemas import WaitWindow, WorkflowDefinition, WorkflowNode
from app.crm.outreach.window import alarm

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)  # 17:30 IST


def _old_board(word: str = "wait_event") -> Dict[str, Any]:
    """A document as stored before the ruling: a listening square spelled
    wait_event, its arrows labelled by the answer and the timer."""
    return {
        "entry": {"topic": "orders/create"},
        "nodes": [
            {
                "id": "ask",
                "type": word,
                "topics": ["button.reply"],
                "key": "button_id",
                "minutes": 60,
            },
            {"id": "confirmed", "type": "wait", "minutes": 1440},
            {"id": "late", "type": "wait", "minutes": 1440},
        ],
        "edges": [["ask", "confirmed", "YES"], ["ask", "late", "timeout"]],
        "goals": [{"topics": ["order.confirmed"]}],
    }


def test_a_stored_wait_event_reads_as_a_listening_wait() -> None:
    ask = WorkflowDefinition.model_validate(_old_board()).nodes[0]
    assert ask.type == "wait"
    assert (listens(ask), branches(ask)) == (True, True)
    assert ask.topics == ["button.reply"] and ask.key == "button_id"


def test_a_new_document_may_not_say_wait_event() -> None:
    problems = validate_definition(_old_board())
    assert problems == [
        "node ask: wait_event is retired — write type wait with its topics "
        "(a wait that lists topics listens)"
    ]
    assert validate_definition(_old_board("wait")) == []


def test_the_ladder_mints_the_one_word() -> None:
    board = expand_stages(
        {
            "stages": {
                "order": ["loan.kyc", "loan.offer"],
                "idle_minutes": 30,
                "on_idle": {"type": "call", "template_id": "tpl-1"},
                "after_action_minutes": 1440,
            },
            "goals": [{"topics": ["loan.disbursed"]}],
        }
    )
    assert "wait_event" not in {node["type"] for node in board["nodes"]}


# --- minutes is optional -------------------------------------------------------

_HOURS = WaitWindow.model_validate(
    {"opens": "09:00", "closes": "17:00", "timezone": "Asia/Kolkata"}
)
RUN_ENDS = NOW + timedelta(days=7)


def test_a_windowed_wait_without_minutes_waits_for_the_opening() -> None:
    """ "Wait until morning": 17:30 IST is shut, so 09:00 IST tomorrow; inside
    the hours it is already due."""
    until_morning = WorkflowNode(id="m", type="wait", window=_HOURS)
    assert alarm(until_morning, NOW, RUN_ENDS) == datetime(
        2026, 9, 18, 3, 30, tzinfo=timezone.utc
    )
    morning = datetime(2026, 9, 18, 5, 0, tzinfo=timezone.utc)  # 10:30 IST
    assert alarm(until_morning, morning, RUN_ENDS) == morning


def test_a_listening_wait_without_minutes_listens_as_long_as_the_run_lives() -> None:
    """Just past the run's life, so the walker's max-age check ends it as
    timed_out rather than the timer taking an arrow."""
    listen = WorkflowNode(id="l", type="wait", topics=["x"], key="$topic")
    assert alarm(listen, NOW, RUN_ENDS) == RUN_ENDS + timedelta(minutes=1)
    assert alarm(listen, RUN_ENDS + timedelta(hours=1), RUN_ENDS) == (
        RUN_ENDS + timedelta(hours=1, minutes=1)
    )


def test_a_listening_wait_with_a_window_and_no_minutes_listens_for_life_then_acts_in_hours() -> (
    None
):
    """The two fields answer different questions: topics say HOW LONG (the
    run's life), the window says WHEN THAT MAY ACT. Arriving while the hours
    are open must not fire at once — it listens."""
    listen_in_hours = WorkflowNode(
        id="l", type="wait", topics=["x"], key="$topic", window=_HOURS
    )
    arrived = datetime(2026, 9, 18, 5, 0, tzinfo=timezone.utc)  # 10:30 IST, open
    life_ends_open = arrived + timedelta(days=7)  # 10:30 IST, open
    wake = alarm(listen_in_hours, arrived, life_ends_open)
    assert wake != arrived
    assert wake == life_ends_open + timedelta(minutes=1)
    life_ends_shut = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)  # 17:30 IST
    assert alarm(listen_in_hours, arrived, life_ends_shut) == datetime(
        2026, 9, 26, 3, 30, tzinfo=timezone.utc
    )  # the next 09:00 IST after the run's life


def _one_wait(node: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "entry": {"topic": "orders/create"},
        "nodes": [node, {"id": "after", "type": "wait", "minutes": 60}],
        "edges": [[node["id"], "after", *(["timeout"] if node.get("topics") else [])]],
        "goals": [{"topics": ["order.paid"]}],
    }


def test_minutes_may_be_left_out_only_when_something_else_sets_the_alarm() -> None:
    listen = {"id": "w", "type": "wait", "topics": ["x"], "key": "$topic"}
    until = {"id": "w", "type": "wait", "window": _HOURS.model_dump()}
    nothing = {"id": "w", "type": "wait"}
    assert validate_definition(_one_wait(listen)) == []
    assert validate_definition(_one_wait(until)) == []
    assert any(
        "waits for nothing" in p for p in validate_definition(_one_wait(nothing))
    )


def test_a_key_belongs_to_a_wait_that_lists_topics() -> None:
    keyed = {"id": "w", "type": "wait", "minutes": 5, "key": "$topic"}
    assert any(
        "key belongs to a wait" in p for p in validate_definition(_one_wait(keyed))
    )


def test_an_unenumerable_listening_wait_is_found_by_its_topics_not_its_word() -> None:
    """#1114's leftover: this read matched the type string `wait_event`, so a
    listening `wait` would have slipped past it while listened_facts saw it."""
    definition = WorkflowDefinition.model_validate(
        _one_wait({"id": "w", "type": "wait", "topics": ["x.unknown"], "key": "$topic"})
    )
    assert unenumerable_squares(definition, {"x.unknown": None}) == {"w"}
