"""Monthly plan credits: two pots (the plan's monthly limit with a
used-this-month counter, and the prepaid balance), the credit check, the
locked deduction, and setting a plan."""

from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.database.accessor.breeze_buddy import wallets as wallets_accessor
from app.schemas.breeze_buddy.wallets import WalletResponse
from app.services.breeze_buddy.wallet import deduction, plan as wallet_plan

NOV = date(2026, 11, 1)
DEC = date(2026, 12, 1)
NOW_NOV = datetime(2026, 11, 20, tzinfo=timezone.utc)
NOW_DEC = datetime(2026, 12, 2, tzinfo=timezone.utc)
FUTURE = datetime(2999, 1, 1, tzinfo=timezone.utc)
PAST = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _wallet(
    balance=0, limit=0, used=0, status="NONE", period=None, trial=None
) -> WalletResponse:
    return WalletResponse(
        merchant_id="m1",
        balance_credits=Decimal(balance),
        conversion_rate=Decimal(1),
        monthly_credit_limit=Decimal(limit),
        plan_used_credits=Decimal(used),
        subscription_status=status,
        last_reset_date=period,
        trial_ends_at=trial,
    )


class TestPeriod:
    def test_first_of_the_month_in_utc(self):
        # 1 Dec 03:00 IST is still 30 Nov in UTC.
        late_nov_utc = datetime(2026, 11, 30, 21, 30, tzinfo=timezone.utc)
        assert wallet_plan.current_period(late_nov_utc) == NOV


class TestMonthlyCounter:
    def test_used_counts_only_this_month(self):
        w = _wallet(0, 10000, 300, "ACTIVE", NOV)
        assert wallet_plan.used_this_month(w, NOV) == 300
        assert wallet_plan.used_this_month(w, DEC) == 0

    def test_plan_refills_on_the_1st_without_a_write(self):
        w = _wallet(0, 10000, 10000, "ACTIVE", NOV)
        assert wallet_plan.plan_left(w, NOV) == 0
        assert wallet_plan.plan_left(w, DEC) == 10000

    def test_upgrade_adds_the_difference_used_stays_used(self):
        # LITE 10,000 with 300 used -> PRO 20,000: 19,700 left, like voice.
        w = _wallet(0, 20000, 300, "ACTIVE", NOV)
        assert wallet_plan.plan_left(w, NOV) == 19700

    def test_downgrade_below_usage_leaves_nothing_never_negative(self):
        w = _wallet(0, 5000, 7000, "ACTIVE", NOV)
        assert wallet_plan.plan_left(w, NOV) == 0

    def test_available_is_plan_left_plus_prepaid_balance(self):
        w = _wallet(3000, 10000, 9000, "ACTIVE", NOV)
        assert wallet_plan.available(w, NOV) == 4000

    def test_prepaid_wallet_is_just_its_balance(self):
        assert wallet_plan.available(_wallet(42), DEC) == 42


class TestSplitCharge:
    def test_plan_first(self):
        c = wallet_plan.split_charge(
            _wallet(50, 10, 0, "ACTIVE", NOV), Decimal(5), NOV, NOW_NOV
        )
        assert (c.from_plan, c.from_balance) == (5, 0)

    def test_then_the_balance_when_the_plan_runs_out(self):
        c = wallet_plan.split_charge(
            _wallet(50, 10, 8, "ACTIVE", NOV), Decimal(5), NOV, NOW_NOV
        )
        assert (c.from_plan, c.from_balance) == (2, 3)

    def test_last_months_usage_does_not_count(self):
        c = wallet_plan.split_charge(
            _wallet(50, 10, 10, "ACTIVE", NOV), Decimal(5), DEC, NOW_DEC
        )
        assert (c.from_plan, c.from_balance) == (5, 0)

    def test_free_during_a_trial(self):
        c = wallet_plan.split_charge(
            _wallet(0, 10, 10, "ACTIVE", NOV, FUTURE), Decimal(5), NOV, NOW_NOV
        )
        assert (c.from_plan, c.from_balance) == (0, 0)

    def test_prepaid_wallet_pays_from_its_balance(self):
        c = wallet_plan.split_charge(_wallet(50), Decimal(5), NOV, NOW_NOV)
        assert (c.from_plan, c.from_balance) == (0, 5)


class TestGate:
    def test_frozen_blocks_others_check_credits(self):
        assert wallet_plan.plan_gate(_wallet(9, 10, 0, "FROZEN", DEC)) is False
        assert wallet_plan.plan_gate(_wallet(9, 10, 0, "ACTIVE", DEC)) is None
        assert wallet_plan.plan_gate(_wallet(9)) is None

    def test_trial_is_allowed_even_when_frozen_like_voice(self):
        assert wallet_plan.plan_gate(_wallet(0, 10, 0, "FROZEN", DEC, FUTURE)) is True

    def test_ended_trial_counts_as_no_trial(self):
        assert wallet_plan.plan_gate(_wallet(0, 10, 0, "ACTIVE", DEC, PAST)) is None
        assert wallet_plan.plan_gate(_wallet(0, 10, 0, "FROZEN", DEC, PAST)) is False


class TestCreditCheck:
    """Read-only: no lock, no write."""

    @pytest.fixture
    def wallet_is(self, monkeypatch):
        def install(w):
            async def _get(_mid):
                return w

            monkeypatch.setattr(deduction, "get_wallet", _get)
            monkeypatch.setattr(wallet_plan, "current_period", lambda *_: NOV)

        return install

    @pytest.mark.asyncio
    async def test_frozen_blocks_even_with_credits(self, wallet_is):
        wallet_is(_wallet(9000, 10000, 0, "FROZEN", NOV))
        assert await deduction.has_sufficient_credits("m1") is False

    @pytest.mark.asyncio
    async def test_trial_allows_with_nothing_left(self, wallet_is):
        wallet_is(_wallet(0, 10, 10, "ACTIVE", NOV, FUTURE))
        assert await deduction.has_sufficient_credits("m1") is True

    @pytest.mark.asyncio
    async def test_plan_used_up_falls_back_to_the_balance(self, wallet_is):
        wallet_is(_wallet(0, 10000, 10000, "ACTIVE", NOV))
        assert await deduction.has_sufficient_credits("m1") is False
        wallet_is(_wallet(3, 10000, 10000, "ACTIVE", NOV))
        assert await deduction.has_sufficient_credits("m1") is True

    @pytest.mark.asyncio
    async def test_price_counts_plan_and_balance_together(self, wallet_is):
        wallet_is(_wallet(2, 10, 7, "ACTIVE", NOV))  # 3 plan + 2 balance
        assert await deduction.has_sufficient_credits("m1", "try_on") is True
        assert await deduction.has_sufficient_credits("m1", "try_on", floor=1) is False

    @pytest.mark.asyncio
    async def test_prepaid_wallet_unchanged(self, wallet_is):
        wallet_is(_wallet(0))
        assert await deduction.has_sufficient_credits("m1") is False
        wallet_is(_wallet(3))
        assert await deduction.has_sufficient_credits("m1") is True

    @pytest.mark.asyncio
    async def test_unknown_event_raises_even_for_frozen_or_trial(self, wallet_is):
        from app.services.breeze_buddy.wallet.exceptions import UnknownEventTypeError

        for w in (
            _wallet(100, 1000, 0, "FROZEN", NOV),
            _wallet(100, 1000, 0, "ACTIVE", NOV, FUTURE),
        ):
            wallet_is(w)
            with pytest.raises(UnknownEventTypeError):
                await deduction.has_sufficient_credits("m1", "no_such_event")


class _FakeConn:
    """Answers lock -> (usage) -> balance+ledger, recording what ran."""

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
            return [
                {
                    "id": 1,
                    "merchant_id": "m1",
                    "type": values[2],
                    "credits_delta": values[0],
                    "credit_balance_after": self.row["balance_credits"],
                    "amount": None,
                    "currency": None,
                    "gateway": values[5],
                    "gateway_ref_id": values[6],
                    "made_by": None,
                    "created_at": None,
                }
            ]
        if "SET monthly_credit_limit" in q:
            if not self.row:
                return []
            self.row.update(
                monthly_credit_limit=values[0],
                subscription_status=values[1],
                trial_ends_at=values[2],
            )
            return [self.row]
        raise AssertionError(q)

    async def execute(self, query, *values):
        q = " ".join(query.split())
        self.statements.append(q)
        assert "SET plan_used_credits" in q, q
        self.row.update(plan_used_credits=values[0], last_reset_date=values[1])


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


def _row(balance=0, limit=0, used=0, status="NONE", period=None, trial=None):
    return {
        "merchant_id": "m1",
        "reseller_id": None,
        "balance_credits": Decimal(balance),
        "conversion_rate": Decimal(1),
        "monthly_credit_limit": Decimal(limit),
        "plan_used_credits": Decimal(used),
        "subscription_status": status,
        "last_reset_date": period,
        "trial_ends_at": trial,
        "created_at": None,
        "updated_at": None,
    }


class TestDeduct:
    @pytest.mark.asyncio
    async def test_plan_covers_it_balance_untouched(self, fake_db):
        conn = fake_db(_row(500, 10000, 0, "ACTIVE"))

        txn = await deduction.deduct("m1", "chat_turn", "s1:1")

        assert txn.credits_delta == 0
        assert conn.row["balance_credits"] == 500
        assert conn.row["plan_used_credits"] == 1
        assert "FOR UPDATE" in conn.statements[0]
        assert "SET plan_used_credits" in conn.statements[1]
        assert conn.statements[2].startswith("WITH locked AS")

    @pytest.mark.asyncio
    async def test_plan_used_up_takes_from_the_balance(self, fake_db, monkeypatch):
        monkeypatch.setattr(wallet_plan, "current_period", lambda *_: NOV)
        conn = fake_db(_row(500, 10, 8, "ACTIVE", NOV))

        txn = await deduction.deduct("m1", "try_on", "t1")

        assert txn.credits_delta == -3
        assert conn.row["balance_credits"] == 497
        assert conn.row["plan_used_credits"] == 10

    @pytest.mark.asyncio
    async def test_new_month_starts_the_counter_again(self, fake_db, monkeypatch):
        monkeypatch.setattr(wallet_plan, "current_period", lambda *_: DEC)
        conn = fake_db(_row(0, 10, 10, "ACTIVE", NOV))

        await deduction.deduct("m1", "chat_turn", "s1:2")

        assert conn.row["plan_used_credits"] == 1
        assert conn.row["last_reset_date"] == DEC

    @pytest.mark.asyncio
    async def test_free_during_a_trial(self, fake_db):
        conn = fake_db(_row(0, 10, 10, "ACTIVE", NOV, FUTURE))

        txn = await deduction.deduct("m1", "chat_turn", "s1:3")

        assert txn.credits_delta == 0
        assert conn.row["balance_credits"] == 0

    @pytest.mark.asyncio
    async def test_prepaid_wallet_as_before_no_usage_write(self, fake_db):
        conn = fake_db(_row(50))

        txn = await deduction.deduct("m1", "chat_turn", "s1:4")

        assert txn.credits_delta == -1
        assert conn.row["balance_credits"] == 49
        assert not any("SET plan_used_credits" in s for s in conn.statements)

    @pytest.mark.asyncio
    async def test_missing_wallet_raises(self, fake_db):
        fake_db(None)
        with pytest.raises(ValueError):
            await deduction.deduct("nope", "chat_turn", "x")


class TestSetPlan:
    @pytest.mark.asyncio
    async def test_only_the_plan_is_stored_no_credits_move(self, fake_db):
        conn = fake_db(_row(3000, 10000, 300, "ACTIVE", NOV))

        w = await wallet_plan.set_plan("m1", Decimal(20000), "ACTIVE", made_by="admin")

        assert (w.monthly_credit_limit, w.balance_credits, w.plan_used_credits) == (
            20000,
            3000,
            300,
        )
        assert len(conn.statements) == 1
        assert "SET monthly_credit_limit" in conn.statements[0]

    @pytest.mark.asyncio
    async def test_top_ups_survive_plan_changes_and_the_new_month(self, fake_db):
        conn = fake_db(_row(20000))
        await wallet_plan.set_plan("m1", Decimal(5000), "ACTIVE")
        conn.row["balance_credits"] += 3000  # a recharge
        await wallet_plan.set_plan("m1", Decimal(10000), "ACTIVE")

        w = wallets_accessor.decode_wallet(conn.row)
        assert w.balance_credits == 23000
        assert wallet_plan.available(w, DEC) == 33000

    @pytest.mark.asyncio
    async def test_missing_wallet_raises(self, fake_db):
        fake_db(None)
        with pytest.raises(ValueError):
            await wallet_plan.set_plan("nope", Decimal(5000), "ACTIVE")


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
