BEGIN;

CREATE TABLE IF NOT EXISTS wallets (
    merchant_id VARCHAR(255) PRIMARY KEY REFERENCES merchants(merchant_id),
    reseller_id VARCHAR(255),
    balance_credits NUMERIC(14, 4) NOT NULL DEFAULT 0,
    conversion_rate NUMERIC(6, 2) NOT NULL DEFAULT 1.0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
);

-- Backfill a wallet row for every merchant that already existed before this
-- migration ran. New merchants get a wallet automatically via
-- create_wallet_on_conn() inside create_merchant()'s transaction.
INSERT INTO wallets (merchant_id, reseller_id, balance_credits, conversion_rate, created_at, updated_at)
SELECT merchant_id, reseller_id, 0, 1.0, NOW(), NOW()
FROM merchants
ON CONFLICT (merchant_id) DO NOTHING;

-- No FK on merchant_id: wallet_transactions is an append-only ledger that
-- must outlive its parent wallet/merchant rows (both can be hard-deleted
-- once a wallet's balance reaches 0, per delete_merchant()'s business
-- logic). The column stays indexed for lookups, just not FK-constrained.
CREATE TABLE IF NOT EXISTS wallet_transactions (
    id BIGSERIAL PRIMARY KEY,
    merchant_id VARCHAR(255) NOT NULL,
    type VARCHAR(20) NOT NULL CHECK (type IN ('addition', 'deduction')),
    credits_delta NUMERIC(14, 4) NOT NULL,
    credit_balance_after NUMERIC(14, 4) NOT NULL,
    amount NUMERIC(14, 4),
    currency VARCHAR(3),
    gateway VARCHAR(50),
    gateway_ref_id VARCHAR(255),
    usage_metadata JSONB,
    made_by VARCHAR(255),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    UNIQUE (gateway, gateway_ref_id)
);

CREATE INDEX IF NOT EXISTS idx_wallet_txn_merchant_created ON wallet_transactions(merchant_id, created_at DESC);
-- Note: no separate index on (gateway, gateway_ref_id) here — the UNIQUE
-- constraint above already creates that index automatically.

COMMIT;
