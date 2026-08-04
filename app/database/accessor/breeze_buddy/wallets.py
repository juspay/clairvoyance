"""
Database accessor functions for wallet + wallet_transactions management.

This module provides business logic for wallet operations, using queries
from queries.breeze_buddy.wallets and decoders from decoder.breeze_buddy.wallets.
"""

from decimal import Decimal
from typing import Optional

import asyncpg

from app.core.logger import logger
from app.database import get_db_connection
from app.database.decoder.breeze_buddy.wallets import (
    decode_wallet,
    decode_wallet_transaction,
)
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.wallets import (
    apply_wallet_delta_query,
    create_wallet_query,
    delete_wallet_query,
    get_wallet_for_update_query,
    get_wallet_query,
    update_wallet_reseller_id_query,
)
from app.schemas.breeze_buddy.wallets import WalletResponse, WalletTransactionResponse


async def create_wallet_on_conn(
    conn: asyncpg.Connection, merchant_id: str, reseller_id: Optional[str] = None
) -> None:
    """Create a wallet row for a newly created merchant.

    Intended to be called inside the same transaction used by
    accessor.breeze_buddy.merchants.create_merchant, so a merchant can
    never exist without a corresponding wallet row.
    """
    query, values = create_wallet_query(merchant_id, reseller_id)
    await conn.execute(query, *values)


async def update_wallet_reseller_id_on_conn(
    conn: asyncpg.Connection, merchant_id: str, reseller_id: Optional[str]
) -> None:
    """Sync a wallet's denormalized reseller_id after a merchant's
    reseller_id is reassigned.

    Intended to be called inside the same transaction used by
    accessor.breeze_buddy.merchants.update_merchant. No-ops silently if the
    merchant has no wallet row (should not happen in practice).
    """
    query, values = update_wallet_reseller_id_query(merchant_id, reseller_id)
    await conn.execute(query, *values)


async def get_wallet(merchant_id: str) -> Optional[WalletResponse]:
    """Fetch a merchant's wallet (no locking).

    Args:
        merchant_id: The merchant identifier to look up

    Returns:
        WalletResponse if found, None otherwise
    """
    query, values = get_wallet_query(merchant_id)

    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        return decode_wallet(row) if row else None
    except Exception as e:
        logger.error(f"Error fetching wallet for merchant {merchant_id}: {e}")
        raise


async def get_wallet_for_update_on_conn(
    conn: asyncpg.Connection, merchant_id: str
) -> Optional[WalletResponse]:
    """Fetch and lock (SELECT ... FOR UPDATE) a merchant's wallet on an
    existing connection/transaction owned by the caller.

    Intended for use inside accessor.breeze_buddy.merchants.delete_merchant's
    transaction, to safely read balance_credits before deciding whether the
    merchant (and its wallet) may be deleted.
    """
    query, values = get_wallet_for_update_query(merchant_id)
    rows = await conn.fetch(query, *values)
    row = rows[0] if rows else None
    return decode_wallet(row) if row else None


async def delete_wallet_on_conn(conn: asyncpg.Connection, merchant_id: str) -> None:
    """Delete a wallet row on an existing connection/transaction owned by
    the caller.

    Callers MUST verify balance_credits == 0 first (e.g. via
    get_wallet_for_update_on_conn in the same transaction). wallet_transactions
    rows are never touched -- the ledger has no FK to wallets and is
    intentionally preserved as a permanent append-only record.
    """
    query, values = delete_wallet_query(merchant_id)
    await conn.execute(query, *values)


async def apply_recharge(
    merchant_id: str,
    credits_delta: Decimal,
    amount: Decimal,
    currency: str,
    gateway: str,
    gateway_ref_id: str,
    made_by: Optional[str] = None,
) -> WalletTransactionResponse:
    """Recharge a merchant's wallet and append a ledger row.

    Uses apply_wallet_delta_query's single atomic UPDATE...RETURNING + INSERT
    CTE statement, so the row lock Postgres takes internally is held for one
    round trip only (no separate SELECT ... FOR UPDATE followed by
    application-level arithmetic followed by a second UPDATE). This "narrow
    transaction" shape minimizes contention on a single merchant's wallet row
    under concurrent recharge/deduction calls.

    The number of credits to add (credits_delta) must already be computed by
    the caller (see services.breeze_buddy.wallet.conversion.compute_credits) -- this
    accessor has no knowledge of currency conversion. Likewise, gateway and
    gateway_ref_id must be supplied by the caller -- this accessor has no
    knowledge of which payment gateway (or "manual") initiated the recharge,
    so it never needs to change as new gateways are added.

    Raises:
        ValueError: if no wallet exists for merchant_id or a database
            connection could not be acquired.
        Exception: on other database errors.
    """
    try:
        query, values = apply_wallet_delta_query(
            merchant_id=merchant_id,
            type_="addition",
            credits_delta=credits_delta,
            amount=amount,
            currency=currency,
            gateway=gateway,
            gateway_ref_id=gateway_ref_id,
            made_by=made_by,
        )
        async for conn in get_db_connection():
            async with conn.transaction():
                rows = await conn.fetch(query, *values)

            if not rows:
                raise ValueError(f"No wallet found for merchant '{merchant_id}'")

            txn_row = rows[0]
            logger.info(
                f"Recharged wallet for merchant {merchant_id}: "
                f"+{credits_delta} credits "
                f"(balance now {txn_row['credit_balance_after']})"
            )
            return decode_wallet_transaction(txn_row)
        raise ValueError("Failed to acquire database connection")
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"Error recharging wallet for merchant {merchant_id}: {e}")
        raise


async def apply_deduction(
    merchant_id: str,
    credits_delta: Decimal,
    gateway: str,
    gateway_ref_id: str,
) -> WalletTransactionResponse:
    """Deduct credits from a merchant's wallet and append a ledger row.

    ``credits_delta`` must already be negative (the caller decides the
    sign/amount via services.breeze_buddy.wallet.deduction.BILLING_RULES) -- this
    accessor has no knowledge of billing rules. gateway is the event_type
    (e.g. "chat_turn") and gateway_ref_id is the caller-supplied idempotency
    key (e.g. the chat_message.id of the triggering user turn) -- the
    existing UNIQUE(gateway, gateway_ref_id) constraint on wallet_transactions
    gives duplicate-deduction protection for free, the same mechanism that
    protects recharges against webhook retries.

    Uses the same single atomic UPDATE...RETURNING + INSERT CTE statement as
    apply_recharge, for the same narrow-transaction/low-contention reasons.

    No balance floor is enforced here -- zero/negative balance blocking is
    explicitly out of scope for this phase
    (see services.breeze_buddy.wallet.deduction).

    Raises:
        ValueError: if no wallet exists for merchant_id or a database
            connection could not be acquired.
        Exception: on other database errors (including
            asyncpg.UniqueViolationError if gateway_ref_id was already used --
            duplicate deduction attempt).
    """
    try:
        query, values = apply_wallet_delta_query(
            merchant_id=merchant_id,
            type_="deduction",
            credits_delta=credits_delta,
            amount=None,
            currency=None,
            gateway=gateway,
            gateway_ref_id=gateway_ref_id,
            made_by=None,
        )
        async for conn in get_db_connection():
            async with conn.transaction():
                rows = await conn.fetch(query, *values)

            if not rows:
                raise ValueError(f"No wallet found for merchant '{merchant_id}'")

            txn_row = rows[0]
            logger.info(
                f"Deducted wallet for merchant {merchant_id}: "
                f"{credits_delta} credits "
                f"(balance now {txn_row['credit_balance_after']})"
            )
            return decode_wallet_transaction(txn_row)
        raise ValueError("Failed to acquire database connection")
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"Error deducting wallet for merchant {merchant_id}: {e}")
        raise
