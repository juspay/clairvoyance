"""A reply wakes only the run it answers.

The incident, 10 Sep 2026: one customer with several open COD orders, so
several live runs of the confirmation board. Her first tap resolved EVERY
one of them, and a run standing on a CANCEL arrow cancelled a confirmed
order on Shopify.

Nothing in the plan could have prevented it. A square narrows a letter with
`match {payload, run}` — a field of the letter against a field of the run —
and for a WhatsApp reply there was no comparable pair: the letter carries
the provider's id for the message it answers, the run carried OUR id for it,
issued before the provider's existed.

The manifest already knew. It records what caused each send (T16 col 7/8)
under the provider's own id for it (col 14, migration 056's partial UNIQUE,
whose canon note is "how an inbound receipt finds this row"). So a reply is
ADDRESSED, in one indexed read, and nobody declares anything: these plans
carry no `match` at all.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

import pytest

import app.crm.outreach.definitions as definitions
import app.crm.outreach.entry as entry
import app.crm.outreach.reply_attribution as attribution
from app.crm.connectivity.schemas.message import SendBehind
from app.crm.outreach.entry import consume_attributed_event
from app.crm.outreach.schemas import EnrollmentRun, Workflow, WorkflowDefinition
from app.crm.record.schemas import RawEvent

NOW = datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)

#: No `match` anywhere: the point is that none is needed.
_CONFIRM_PLAN: Dict[str, Any] = {
    "entry": {"topic": "orders/create", "key": "id"},
    "goals": [{"topics": ["orders/cancelled"]}],
    "purpose_key": "utility.order.confirmation",
    "nodes": [
        {
            "id": "confirm",
            "type": "send",
            "channel": "whatsapp",
            "template": "cod_confirm_1",
            "variables": {},
        },
        {
            "id": "await-reply",
            "type": "wait_event",
            "topics": ["message.inbound"],
            "key": "reply",
            "minutes": 1440,
        },
        {
            "id": "chase",
            "type": "wait_event",
            "topics": ["message.inbound"],
            "key": "reply",
            "minutes": 1440,
        },
    ],
    "edges": [["confirm", "await-reply"], ["await-reply", "chase", "timeout"]],
}


def _flow() -> Workflow:
    return Workflow(
        id=uuid4(),
        merchant_id="m1",
        name="cod-confirm",
        status="live",
        version=1,
        created_by=None,
        created_at=NOW,
        updated_at=NOW,
        definition=_CONFIRM_PLAN,
        draft=None,
    )


def _run(flow: Workflow, node: str = "await-reply", key: str = "o-1") -> EnrollmentRun:
    return EnrollmentRun(
        id=uuid4(),
        merchant_id="m1",
        workflow_id=flow.id,
        workflow_version=1,
        customer_id=uuid4(),
        status="waiting",
        current_node=node,
        wake_at=NOW + timedelta(minutes=30),
        entered_at=NOW - timedelta(hours=1),
        exited_at=None,
        exit_reason=None,
        context={"phone": "+919876543210"},
        enrollment_key=key,
        attempts=0,
        last_error=None,
    )


def _tap(replied_to: Optional[str] = "wamid.SENT-BY-RUN-A") -> RawEvent:
    """Her CONFIRM tap. A template quick-reply carries context.id — the
    provider's id of the message she answered — which is the thread."""
    message: Dict[str, Any] = {
        "from": "919876543210",
        "id": "wamid.HER-TAP",
        "type": "button",
        "button": {"payload": "CONFIRM", "text": "Confirm"},
    }
    if replied_to is not None:
        message["context"] = {"id": replied_to}
    return RawEvent(
        id="e-1",
        merchant_id="m1",
        source="whatsapp",
        topic="message.inbound",
        schema_version="v23.0",
        external_id="wamid.HER-TAP",
        payload={"messaging_product": "whatsapp", "messages": [message]},
        received_at=NOW,
        occurred_at=NOW,
    )


class _Spine:
    def __init__(self, flow: Workflow, runs: List[EnrollmentRun]) -> None:
        self.flow = flow
        self.runs = runs
        self.resumes: List[Tuple[str, str]] = []

    async def live_workflows(self, merchant_id: str) -> List[Workflow]:
        return []

    async def open_runs_for_customer(self, merchant_id, customer_id):
        return list(self.runs)

    async def get_definition(self, merchant_id, workflow_id, version):
        return _CONFIRM_PLAN

    async def cancel_run(self, *args, **kwargs) -> bool:
        return True

    async def resume_run_by_id(
        self, merchant_id, run_id, node_id, patch, facts=None
    ) -> bool:
        self.resumes.append((run_id, node_id))
        return True


def _install(
    monkeypatch: pytest.MonkeyPatch,
    spine: _Spine,
    behind: Optional[SendBehind],
) -> List[str]:
    """The spine, plus the ONE read attribution makes. `behind` is what the
    manifest says about the id the tap threads to; the list collects the ids
    asked for, so a test can prove the read happened once per letter."""
    definitions._definitions.clear()
    for module, name in (
        (entry.workflow_accessor, "live_workflows"),
        (entry.enrollment_accessor, "open_runs_for_customer"),
        (definitions.version_accessor, "get_definition"),
        (entry.enrollment_accessor, "cancel_run"),
        (entry.enrollment_accessor, "resume_run_by_id"),
    ):
        monkeypatch.setattr(module, name, getattr(spine, name))

    asked: List[str] = []

    async def send_behind(merchant_id: str, provider_message_id: str):
        """Test double: the manifest's own answer, one indexed read."""
        asked.append(provider_message_id)
        return behind

    monkeypatch.setattr(attribution, "send_behind", send_behind)
    return asked


def _consume(event: RawEvent) -> None:
    asyncio.run(consume_attributed_event(event, "c-1", {}))


# --- the incident -------------------------------------------------------------


def test_one_tap_resolves_one_run_and_not_her_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE incident, as a test. Three open orders, three live runs, all of
    them standing on a square that listens for her reply. She taps CONFIRM
    on one message. Before attribution every run took it — and a run whose
    CANCEL arrow led to a cancellation cancelled a confirmed order.

    Note what the plan does NOT contain: a `match`. The letter names the
    message she answered, the manifest names the run that sent it, and
    nothing was declared by anyone."""
    flow = _flow()
    hers, her_other, her_third = (
        _run(flow, key="o-1"),
        _run(flow, key="o-2"),
        _run(flow, key="o-3"),
    )
    spine = _Spine(flow, [hers, her_other, her_third])
    asked = _install(
        monkeypatch,
        spine,
        SendBehind(
            source_kind="workflow",
            source_id=str(hers.id),
            dedupe_key=f"{hers.id}:confirm",
        ),
    )

    _consume(_tap())

    # HER run, and only hers. (Both of its listening squares are offered the
    # letter; the resume statement moves the run only if its token is
    # standing on one, which is how it has always worked.)
    assert {run for run, _ in spine.resumes} == {str(hers.id)}
    assert str(her_other.id) not in {run for run, _ in spine.resumes}
    assert str(her_third.id) not in {run for run, _ in spine.resumes}
    # One read for the letter, not one per run.
    assert asked == ["wamid.SENT-BY-RUN-A"]


def test_the_answer_reaches_the_listener_not_the_square_that_sent_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The manifest names the square that SENT the message; the square that
    waits for the answer is a different one, the listener its edge leads to.

    So attribution addresses the RUN and stops there. Which of that run's
    squares resolves is already decided, and decided better: the resume
    statement moves a run only while its token is standing on that square,
    as a WHERE rather than a Python branch. Narrowing by the sender here
    would silence the very square that is listening — which is what an
    earlier draft of this did, caught by running it against a plan whose
    send and listener are (as they always are) two different nodes."""
    flow = _flow()
    run = _run(flow, node="await-reply")
    spine = _Spine(flow, [run])
    _install(
        monkeypatch,
        spine,
        SendBehind(
            source_kind="workflow",
            source_id=str(run.id),
            dedupe_key=f"{run.id}:confirm",  # the SEND square
        ),
    )

    _consume(_tap())

    assert spine.resumes == [(str(run.id), "await-reply"), (str(run.id), "chase")]


def test_a_run_that_answered_nothing_of_hers_is_not_silenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """She answers a run that has since ended — its goal fired, it timed out,
    she was ejected. Narrowing to a run nobody is holding would silence the
    letter completely, so it is treated as unaddressed: her other runs hear
    it exactly as they did before. The answer is late, not misdirected."""
    flow = _flow()
    still_open = _run(flow)
    spine = _Spine(flow, [still_open])
    _install(
        monkeypatch,
        spine,
        SendBehind(
            source_kind="workflow",
            source_id=str(uuid4()),  # a run not among the open ones
            dedupe_key=f"{uuid4()}:confirm",
        ),
    )

    _consume(_tap())

    assert spine.resumes == [
        (str(still_open.id), "await-reply"),
        (str(still_open.id), "chase"),
    ]


# --- what stays exactly as it was ---------------------------------------------


def test_a_typed_message_is_not_addressed_and_behaves_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """She types "yes" instead of tapping, so the letter threads to nothing.
    Attribution declines and every listening square hears it, exactly as it
    did before this existed — which is why no published plan has to change."""
    flow = _flow()
    run = _run(flow)
    spine = _Spine(flow, [run])
    asked = _install(monkeypatch, spine, None)

    _consume(_tap(replied_to=None))

    # BOTH listening squares are offered it, which is what the system always
    # did — and the contrast with the test above is the whole feature: a
    # threaded letter narrows to one square, an unthreaded one cannot.
    assert spine.resumes == [
        (str(run.id), "await-reply"),
        (str(run.id), "chase"),
    ]
    assert asked == []  # nothing to look up: no thread on the letter


def test_a_thread_naming_a_send_that_was_not_ours_is_not_addressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """She replied to a message this system never sent (the merchant's own
    inbox, another tool). The manifest holds no such id, so the letter is
    unaddressed rather than attributed to a guess."""
    flow = _flow()
    run = _run(flow)
    spine = _Spine(flow, [run])
    _install(monkeypatch, spine, None)

    _consume(_tap(replied_to="wamid.NOT-OURS"))

    # Unaddressed: every listening square hears it, as it always did.
    assert spine.resumes == [
        (str(run.id), "await-reply"),
        (str(run.id), "chase"),
    ]


@pytest.mark.parametrize("source_kind", ["broadcast", "agent", "transactional"])
def test_a_reply_to_a_send_no_run_made_addresses_no_run(
    monkeypatch: pytest.MonkeyPatch, source_kind: str
) -> None:
    """The message she answered was a broadcast, an agent's, or a
    transactional one. There is no run behind it, and guessing past the
    producer's own word is the cross-wake this prevents."""
    flow = _flow()
    run = _run(flow)
    spine = _Spine(flow, [run])
    _install(
        monkeypatch,
        spine,
        SendBehind(source_kind=source_kind, source_id=str(uuid4()), dedupe_key="b-1"),
    )

    _consume(_tap())

    # Unaddressed, so the pre-existing behaviour stands.
    # Unaddressed: every listening square hears it, as it always did.
    assert spine.resumes == [
        (str(run.id), "await-reply"),
        (str(run.id), "chase"),
    ]


def test_a_dedupe_key_that_is_not_run_colon_node_addresses_nobody(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row's two accounts of which run sent it disagree: source_id says
    one thing (canon T16 col 8) and the producer's own dedupe_key another.
    Acting on either half of a contradiction is how a letter reaches a run
    it is not about, so the letter is left unaddressed."""
    flow = _flow()
    run = _run(flow)
    spine = _Spine(flow, [run])
    _install(
        monkeypatch,
        spine,
        SendBehind(
            source_kind="workflow", source_id=str(run.id), dedupe_key="something-else"
        ),
    )

    _consume(_tap())

    # Unaddressed: every listening square hears it, as it always did.
    assert spine.resumes == [
        (str(run.id), "await-reply"),
        (str(run.id), "chase"),
    ]


def test_a_source_that_declares_no_thread_field_is_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attribution reads the source's own declared `replied_to`. A source
    that declares none (a Shopify letter, a lead push) resolves to nothing,
    so no read is made and nothing about that source changes."""
    flow = _flow()
    run = _run(flow)
    spine = _Spine(flow, [run])
    asked = _install(monkeypatch, spine, None)

    order = RawEvent(
        id="e-2",
        merchant_id="m1",
        source="shopify",
        topic="message.inbound",
        schema_version="1",
        external_id="x-1",
        payload={"id": 5, "reply": "CONFIRM"},
        received_at=NOW,
        occurred_at=NOW,
    )
    _consume(order)

    assert asked == []
    # Unaddressed: every listening square hears it, as it always did.
    assert spine.resumes == [
        (str(run.id), "await-reply"),
        (str(run.id), "chase"),
    ]


def test_the_word_it_filters_on_is_the_word_the_send_queues_with() -> None:
    """Two spellings of one producer word, with nothing joining them: the
    send square passes ``source_kind="workflow"`` to queue_message, and
    attribution filters the manifest's answer on the same string.

    If they ever diverged, attribution would address nothing — every reply
    would fall back to the open default, which is the incident — and no test
    would fail, because each side is individually right. So they are pinned
    to each other, and to the connectivity vocabulary that admits the word.
    """
    import inspect

    from app.crm.connectivity.queue import SOURCE_KINDS
    from app.crm.outreach.nodes import send as send_node

    assert attribution._WORKFLOW in SOURCE_KINDS
    assert f'source_kind="{attribution._WORKFLOW}"' in inspect.getsource(send_node)
