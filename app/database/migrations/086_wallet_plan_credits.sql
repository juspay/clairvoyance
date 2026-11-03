-- 086: wallets — monthly plan credits, the same shape as Breeze Buddy voice's
-- monthly call limit.
--
-- A merchant on a plan gets monthly_credit_limit every calendar month (UTC). The
-- balance is "monthly_credit_limit minus what was used this month": it goes back to
-- monthly_credit_limit on the first credit check of a new month, and a plan change
-- moves it by (new - old) at once. The billing platform (nautilus for
-- Shopify) owns which plan a merchant is on and pushes it here.
--
--   monthly_credit_limit  the monthly limit (voice: monthly_order_limit).
--   subscription_status   'NONE' (no plan: prepaid wallet, never reset, as
--                         before), 'ACTIVE', 'FROZEN' (blocked). Unchecked:
--                         the vocabulary lives in code.
--   last_reset_date       first day (UTC) of the month the balance belongs
--                         to. NULL until a plan is set.
--   trial_ends_at         unlimited before this instant, even when FROZEN --
--                         like voice, the trial is a date checked on every use.
--
-- Names match voice's columns on nautilus breeze_buddy_settings.
ALTER TABLE wallets
    ADD COLUMN IF NOT EXISTS monthly_credit_limit NUMERIC(14, 4) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS subscription_status VARCHAR(20) NOT NULL DEFAULT 'NONE',
    ADD COLUMN IF NOT EXISTS last_reset_date DATE,
    ADD COLUMN IF NOT EXISTS trial_ends_at TIMESTAMPTZ;
