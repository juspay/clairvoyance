"""
Database query functions for wallet + wallet_transactions management.

This module contains ONLY query generation functions.
Business logic should be in accessor/breeze_buddy/wallets.py
"""

from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

WALLETS_TABLE = "wallets"
WALLET_TRANSACTIONS_TABLE = "wallet_transactions"


def create_wallet_query(
    merchant_id: str, reseller_id: Optional[str] = None
) -> Tuple[str, List[Any]]:
    """Generate query to create a wallet row for a newly created merchant."""
    query = f"""
        INSERT INTO {WALLETS_TABLE} (
            merchant_id, reseller_id, balance_credits, conversion_rate,
            created_at, updated_at
        ) VALUES ($1, $2, 0, 1.0, $3, $3)
        RETURNING merchant_id, reseller_id, balance_credits, conversion_rate,
                  created_at, updated_at
    """
    now = datetime.now(timezone.utc)
    return query, [merchant_id, reseller_id, now]


def get_wallet_query(merchant_id: str) -> Tuple[str, List[Any]]:
    """Generate query to fetch a wallet row (no locking)."""
    query = f"""
        SELECT merchant_id, reseller_id, balance_credits, conversion_rate,
               created_at, updated_at
        FROM {WALLETS_TABLE}
        WHERE merchant_id = $1
    """
    return query, [merchant_id]


def get_wallet_for_update_query(merchant_id: str) -> Tuple[str, List[Any]]:
    """Generate query to fetch a wallet row and lock it for the duration of
    the enclosing transaction (SELECT ... FOR UPDATE)."""
    query = f"""
        SELECT merchant_id, reseller_id, balance_credits, conversion_rate,
               created_at, updated_at
        FROM {WALLETS_TABLE}
        WHERE merchant_id = $1
        FOR UPDATE
    """
    return query, [merchant_id]


def update_wallet_reseller_id_query(
    merchant_id: str, reseller_id: Optional[str]
) -> Tuple[str, List[Any]]:
    """Generate query to sync a wallet's denormalized reseller_id after a
    merchant's reseller_id is reassigned."""
    query = f"""
        UPDATE {WALLETS_TABLE}
        SET reseller_id = $1, updated_at = $2
        WHERE merchant_id = $3
        RETURNING merchant_id, reseller_id, balance_credits, conversion_rate,
                  created_at, updated_at
    """
    now = datetime.now(timezone.utc)
    return query, [reseller_id, now, merchant_id]


def update_wallet_balance_query(
    merchant_id: str, new_balance: Any
) -> Tuple[str, List[Any]]:
    """Generate query to set a wallet's cached balance_credits."""
    query = f"""
        UPDATE {WALLETS_TABLE}
        SET balance_credits = $1, updated_at = $2
        WHERE merchant_id = $3
        RETURNING merchant_id, reseller_id, balance_credits, conversion_rate,
                  created_at, updated_at
    """
    now = datetime.now(timezone.utc)
    return query, [new_balance, now, merchant_id]


def delete_wallet_query(merchant_id: str) -> Tuple[str, List[Any]]:
    """Generate query to delete a wallet row.

    Callers must ensure balance_credits == 0 before calling this -- the
    ledger (wallet_transactions) is intentionally left untouched, since it
    has no FK to wallets/merchants and must survive as a permanent
    append-only record regardless of merchant/wallet lifecycle.
    """
    query = f"""
        DELETE FROM {WALLETS_TABLE}
        WHERE merchant_id = $1
        RETURNING merchant_id
    """
    return query, [merchant_id]


def apply_wallet_delta_query(
    merchant_id: str,
    type_: str,
    credits_delta: Any,
    amount: Optional[Any] = None,
    currency: Optional[str] = None,
    gateway: Optional[str] = None,
    gateway_ref_id: Optional[str] = None,
    made_by: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """Generate a single atomic statement that updates a wallet's cached
    balance AND appends the corresponding ledger row, in one round trip.

    Uses a CTE so the row lock Postgres takes internally for the UPDATE is
    held for the shortest possible time (no separate SELECT ... FOR UPDATE,
    no application-level read-modify-write round trip in between). This is
    the "narrow transaction" pattern -- keeping the critical section as
    small as physically possible to minimize contention on a single
    merchant's wallet row under concurrent recharge/deduction calls.

    ``credits_delta`` may be positive (recharge/addition) or negative
    (deduction) -- the caller decides the sign; this function is agnostic
    to direction.
    """
    query = f"""
        WITH locked AS (
            UPDATE {WALLETS_TABLE}
            SET balance_credits = balance_credits + $1,
                updated_at = $9
            WHERE merchant_id = $2
            RETURNING balance_credits AS new_balance
        )
        INSERT INTO {WALLET_TRANSACTIONS_TABLE} (
            merchant_id, type, credits_delta, credit_balance_after,
            amount, currency, gateway, gateway_ref_id, made_by, created_at
        )
        SELECT $2, $3, $1, locked.new_balance, $4, $5, $6, $7, $8, $9
        FROM locked
        RETURNING id, merchant_id, type, credits_delta, credit_balance_after,
                  amount, currency, gateway, gateway_ref_id, made_by, created_at
    """
    now = datetime.now(timezone.utc)
    values = [
        credits_delta,
        merchant_id,
        type_,
        amount,
        currency,
        gateway,
        gateway_ref_id,
        made_by,
        now,
    ]
    return query, values


def insert_wallet_transaction_query(
    merchant_id: str,
    type_: str,
    credits_delta: Any,
    credit_balance_after: Any,
    amount: Optional[Any] = None,
    currency: Optional[str] = None,
    gateway: Optional[str] = None,
    gateway_ref_id: Optional[str] = None,
    made_by: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """Generate query to append a row to the wallet_transactions ledger."""
    query = f"""
        INSERT INTO {WALLET_TRANSACTIONS_TABLE} (
            merchant_id, type, credits_delta, credit_balance_after,
            amount, currency, gateway, gateway_ref_id, made_by, created_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        RETURNING id, merchant_id, type, credits_delta, credit_balance_after,
                  amount, currency, gateway, gateway_ref_id, made_by, created_at
    """
    now = datetime.now(timezone.utc)
    values = [
        merchant_id,
        type_,
        credits_delta,
        credit_balance_after,
        amount,
        currency,
        gateway,
        gateway_ref_id,
        made_by,
        now,
    ]
    return query, values
