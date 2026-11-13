-- 086: wallets — monthly plan credits, the same shape as Breeze Buddy voice's
-- monthly call limit (nautilus breeze_buddy_settings: monthly_order_limit +
-- a used-this-month counter + last_reset_date).
--
-- A wallet has two pots. The plan gives monthly_credit_limit every calendar
-- month (UTC); plan_used_credits counts what the month in last_reset_date
-- used, and an older month counts as 0, so the plan refills on the 1st with
-- no reset write. balance_credits stays the prepaid pot (recharges), never
-- reset. A charge takes from the plan first, then from the balance. The
-- billing platform (nautilus for Shopify) owns the plan and pushes it here.
--
--   monthly_credit_limit  the monthly limit (voice: monthly_order_limit).
--                         0 = no plan credits.
--   plan_used_credits     plan credits used in the month of last_reset_date.
--   subscription_status   'NONE' (no plan: prepaid wallet, as before),
--                         'ACTIVE', 'FROZEN' (blocked). Unchecked: the
--                         vocabulary lives in code.
--   last_reset_date       first day (UTC) of the month plan_used_credits
--                         belongs to. NULL until the first plan charge.
--   trial_ends_at         free and allowed before this instant, even when
--                         FROZEN -- like voice, a date checked on every use.
ALTER TABLE wallets
    ADD COLUMN IF NOT EXISTS monthly_credit_limit NUMERIC(14, 4) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS plan_used_credits NUMERIC(14, 4) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS subscription_status VARCHAR(20) NOT NULL DEFAULT 'NONE',
    ADD COLUMN IF NOT EXISTS last_reset_date DATE,
    ADD COLUMN IF NOT EXISTS trial_ends_at TIMESTAMPTZ;
