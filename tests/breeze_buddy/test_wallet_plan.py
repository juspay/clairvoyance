"""Monthly plan credits: the voice-limit rules, the locked update, and the
credit check that applies them."""

from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.database.accessor.breeze_buddy import wallets as wallets_accessor
from app.schemas.breeze_buddy.wallets import WalletResponse
from app.services.breeze_buddy.wallet import deduction, plan as wallet_plan

NOV = date(2026, 11, 1)
DEC = date(2026, 12, 1)


def _wallet(
    balance, monthly_credit_limit=0, status="NONE", period=None, trial=None
) -> WalletResponse:
    return WalletResponse(
        merchant_id="m1",
        balance_credits=Decimal(balance),
        conversion_rate=Decimal(1),
        monthly_credit_limit=Decimal(monthly_credit_limit),
        subscription_status=status,
        last_reset_date=period,
        trial_ends_at=trial,
    )


FUTURE = datetime(2999, 1, 1, tzinfo=timezone.utc)
PAST = datetime(2000, 1, 1, tzinfo=timezone.utc)


class TestPeriod:
    def test_first_of_the_month_in_utc(self):
        # 1 Dec 03:00 IST is still 30 Nov in UTC.
        late_nov_utc = datetime(2026, 11, 30, 21, 30, tzinfo=timezone.utc)
        assert wallet_plan.current_period(late_nov_utc) == NOV
        assert (
            wallet_plan.current_period(datetime(2026, 12, 1, tzinfo=timezone.utc))
            == DEC
        )


class TestPlanChange:
    def test_first_plan_adds_its_credits(self):
        u = wallet_plan.decide_plan_change(
            _wallet(0), Decimal(5000), "ACTIVE", None, NOV
        )
        assert u.credits_delta == 5000
        assert (u.monthly_credit_limit, u.subscription_status, u.last_reset_date) == (
            5000,
            "ACTIVE",
            NOV,
        )

    def test_upgrade_adds_the_difference_used_stays_used(self):
        # LITE 10,000 with 4,000 used -> PRO: 16,000 left.
        w = _wallet(6000, 10000, "ACTIVE", NOV)
        u = wallet_plan.decide_plan_change(w, Decimal(20000), "ACTIVE", None, NOV)
        assert u.credits_delta == 10000
        assert w.balance_credits + u.credits_delta == 16000

    def test_downgrade_takes_the_difference_now(self):
        # PRO 20,000 with 4,000 used -> LITE: 6,000 left.
        w = _wallet(16000, 20000, "ACTIVE", NOV)
        u = wallet_plan.decide_plan_change(w, Decimal(10000), "ACTIVE", None, NOV)
        assert w.balance_credits + u.credits_delta == 6000

    def test_downgrade_below_usage_goes_negative_like_voice_used_over_limit(self):
        # PRO with 15,000 used -> LITE: 5,000 over; a later upgrade nets out.
        w = _wallet(5000, 20000, "ACTIVE", NOV)
        u = wallet_plan.decide_plan_change(w, Decimal(10000), "ACTIVE", None, NOV)
        assert w.balance_credits + u.credits_delta == -5000

    def test_same_plan_again_moves_nothing(self):
        w = _wallet(7000, 10000, "ACTIVE", NOV)
        u = wallet_plan.decide_plan_change(w, Decimal(10000), "ACTIVE", None, NOV)
        assert u.credits_delta == 0

    def test_change_in_a_new_month_starts_it_on_the_new_plan(self):
        w = _wallet(300, 10000, "ACTIVE", NOV)
        u = wallet_plan.decide_plan_change(w, Decimal(20000), "ACTIVE", None, DEC)
        assert w.balance_credits + u.credits_delta == 20000
        assert u.last_reset_date == DEC

    def test_change_read_before_a_reset_never_moves_the_month_back(self):
        # PUT at 30 Nov 23:59:59 read period=NOV; a chat reset the wallet to
        # DEC before the PUT got the lock. Must stay DEC (same-month change),
        # else the next check redoes DEC's reset and hits its ledger ref.
        w = _wallet(10000, 10000, "ACTIVE", DEC)
        u = wallet_plan.decide_plan_change(w, Decimal(20000), "ACTIVE", None, NOV)
        assert u.last_reset_date == DEC
        assert w.balance_credits + u.credits_delta == 20000
        assert not wallet_plan.needs_reset(_wallet(0, 20000, "ACTIVE", DEC), DEC)


class TestMonthlyReset:
    def test_new_month_resets_to_plan_credits(self):
        w = _wallet(-200, 10000, "ACTIVE", NOV)
        u = wallet_plan.decide_reset(w, DEC)
        assert u is not None
        assert w.balance_credits + u.credits_delta == 10000
        assert u.last_reset_date == DEC

    def test_same_month_does_nothing(self):
        assert wallet_plan.decide_reset(_wallet(10, 10000, "ACTIVE", DEC), DEC) is None

    def test_wallet_without_a_plan_is_never_reset(self):
        assert wallet_plan.decide_reset(_wallet(42), DEC) is None


class TestGate:
    def test_frozen_blocks_others_check_balance(self):
        assert wallet_plan.plan_gate(_wallet(9, 10, "FROZEN", DEC)) is False
        assert wallet_plan.plan_gate(_wallet(9, 10, "ACTIVE", DEC)) is None
        assert wallet_plan.plan_gate(_wallet(9)) is None

    def test_trial_is_unlimited_even_when_frozen_like_voice(self):
        assert wallet_plan.plan_gate(_wallet(0, 10, "ACTIVE", DEC, FUTURE)) is True
        assert wallet_plan.plan_gate(_wallet(0, 10, "FROZEN", DEC, FUTURE)) is True

    def test_ended_trial_counts_as_no_trial(self):
        assert wallet_plan.plan_gate(_wallet(0, 10, "ACTIVE", DEC, PAST)) is None
        assert wallet_plan.plan_gate(_wallet(0, 10, "FROZEN", DEC, PAST)) is False

    def test_trial_end_is_kept_across_a_monthly_reset(self):
        u = wallet_plan.decide_reset(_wallet(0, 10, "ACTIVE", NOV, FUTURE), DEC)
        assert u is not None and u.trial_ends_at == FUTURE


class _FakeConn:
    """Answers lock -> (delta) -> set-plan, recording what ran."""

    def __init__(self, wallet_row):
        self.row: dict = dict(wallet_row or {})
        self.statements: list[str] = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetch(self, query, *values):
        q = " ".join(query.split())
        self.statements.append(q)
        if "FOR UPDATE" in q:
            return [self.row] if self.row else []
        if q.startswith("WITH locked AS"):
            self.row["balance_credits"] += values[0]
            return [{"id": 1}]
        if "SET monthly_credit_limit" in q:
            self.row.update(
                monthly_credit_limit=values[0],
                subscription_status=values[1],
                last_reset_date=values[2],
            )
            return [self.row]
        raise AssertionError(q)


@pytest.fixture
def fake_db(monkeypatch):
    def install(row):
        conn = _FakeConn(row)

        @asynccontextmanager
        async def _connection():
            yield conn

        monkeypatch.setattr(wallets_accessor, "db_connection", _connection)
        return conn

    return install


def _row(balance, monthly_credit_limit, status, period):
    return {
        "merchant_id": "m1",
        "reseller_id": None,
        "balance_credits": Decimal(balance),
        "conversion_rate": Decimal(1),
        "monthly_credit_limit": Decimal(monthly_credit_limit),
        "subscription_status": status,
        "last_reset_date": period,
        "trial_ends_at": None,
        "created_at": None,
        "updated_at": None,
    }


class TestSetPlan:
    @pytest.mark.asyncio
    async def test_upgrade_writes_ledger_then_plan_under_lock(
        self, fake_db, monkeypatch
    ):
        monkeypatch.setattr(wallet_plan, "current_period", lambda: NOV)
        conn = fake_db(_row(6000, 10000, "ACTIVE", NOV))

        w = await wallet_plan.set_plan("m1", Decimal(20000), "ACTIVE", made_by="admin")

        assert (w.balance_credits, w.monthly_credit_limit) == (16000, 20000)
        assert "FOR UPDATE" in conn.statements[0]
        assert conn.statements[1].startswith("WITH locked AS")
        assert "SET monthly_credit_limit" in conn.statements[2]

    @pytest.mark.asyncio
    async def test_status_only_change_writes_no_ledger_row(self, fake_db, monkeypatch):
        monkeypatch.setattr(wallet_plan, "current_period", lambda: NOV)
        conn = fake_db(_row(6000, 10000, "ACTIVE", NOV))

        w = await wallet_plan.set_plan("m1", Decimal(10000), "FROZEN")

        assert w.subscription_status == "FROZEN"
        assert not any(s.startswith("WITH locked AS") for s in conn.statements)

    @pytest.mark.asyncio
    async def test_missing_wallet_raises(self, fake_db):
        fake_db(None)
        with pytest.raises(ValueError):
            await wallet_plan.set_plan("nope", Decimal(5000), "ACTIVE")


class TestCreditCheck:
    @pytest.fixture
    def wallet_is(self, monkeypatch):
        def install(w, reset_to=None):
            async def _get(_mid):
                return w

            async def _reset(_mid):
                return reset_to

            monkeypatch.setattr(deduction, "get_wallet", _get)
            monkeypatch.setattr(wallet_plan, "reset_month", _reset)

        return install

    @pytest.mark.asyncio
    async def test_frozen_blocks_even_with_balance(self, wallet_is):
        wallet_is(_wallet(9000, 10000, "FROZEN", DEC))
        assert await deduction.has_sufficient_credits("m1") is False

    @pytest.mark.asyncio
    async def test_trial_allows_even_at_zero(self, wallet_is):
        wallet_is(_wallet(-50, 10000, "ACTIVE", DEC, FUTURE))
        assert await deduction.has_sufficient_credits("m1") is True

    @pytest.mark.asyncio
    async def test_ended_trial_blocks_at_zero(self, wallet_is):
        wallet_is(_wallet(-50, 10000, "ACTIVE", DEC, PAST))
        assert await deduction.has_sufficient_credits("m1") is False

    @pytest.mark.asyncio
    async def test_new_month_resets_before_checking(self, wallet_is, monkeypatch):
        monkeypatch.setattr(wallet_plan, "current_period", lambda: DEC)
        wallet_is(
            _wallet(0, 10000, "ACTIVE", NOV),
            reset_to=_wallet(10000, 10000, "ACTIVE", DEC),
        )
        assert await deduction.has_sufficient_credits("m1") is True

    @pytest.mark.asyncio
    async def test_prepaid_wallet_unchanged(self, wallet_is):
        wallet_is(_wallet(0))
        assert await deduction.has_sufficient_credits("m1") is False
        wallet_is(_wallet(3))
        assert await deduction.has_sufficient_credits("m1") is True


class TestTrialUsage:
    """Like voice's frozen call counter: trial usage costs no plan credits."""

    def test_only_deductions_are_free_during_a_trial(self):
        from app.database.queries.breeze_buddy.wallets import apply_wallet_delta_query

        free, _ = apply_wallet_delta_query(
            "m1", "deduction", Decimal(-1), free_during_trial=True
        )
        paid, _ = apply_wallet_delta_query("m1", "addition", Decimal(5))
        assert "trial_ends_at > $9" in free
        assert "trial_ends_at" not in paid

    @pytest.mark.asyncio
    async def test_deduct_is_free_during_a_trial(self, monkeypatch):
        seen: dict = {}

        def _query(**kwargs):
            seen.update(kwargs)
            raise RuntimeError("stop before the DB")

        monkeypatch.setattr(wallets_accessor, "apply_wallet_delta_query", _query)
        with pytest.raises(RuntimeError):
            await deduction.deduct("m1", "chat_turn", "s1:1")
        assert seen["free_during_trial"] is True


class TestValidation:
    def test_plan_credits_is_capped(self):
        from pydantic import ValidationError

        from app.schemas.breeze_buddy.wallets import WalletPlanRequest

        WalletPlanRequest(
            monthly_credit_limit=Decimal(1_000_000_000), subscription_status="ACTIVE"
        )
        with pytest.raises(ValidationError):
            WalletPlanRequest(
                monthly_credit_limit=Decimal(1_000_000_001),
                subscription_status="ACTIVE",
            )

    def test_trial_end_needs_a_timezone(self):
        from pydantic import ValidationError

        from app.schemas.breeze_buddy.wallets import WalletPlanRequest

        WalletPlanRequest(
            monthly_credit_limit=1, subscription_status="ACTIVE", trial_ends_at=FUTURE
        )
        with pytest.raises(ValidationError):
            WalletPlanRequest(
                monthly_credit_limit=1,
                subscription_status="ACTIVE",
                trial_ends_at=datetime(2999, 1, 1),
            )
        with pytest.raises(ValidationError):
            WalletPlanRequest.model_validate(
                {"monthly_credit_limit": 1, "subscription_status": "TRIAL"}
            )

    @pytest.mark.asyncio
    async def test_unknown_event_raises_even_for_frozen_or_trial(self, monkeypatch):
        from app.services.breeze_buddy.wallet.exceptions import UnknownEventTypeError

        for w in (
            _wallet(100, 1000, "FROZEN", DEC),
            _wallet(100, 1000, "ACTIVE", DEC, FUTURE),
        ):

            async def _get(_mid, w=w):
                return w

            monkeypatch.setattr(deduction, "get_wallet", _get)
            with pytest.raises(UnknownEventTypeError):
                await deduction.has_sufficient_credits("m1", "no_such_event")
