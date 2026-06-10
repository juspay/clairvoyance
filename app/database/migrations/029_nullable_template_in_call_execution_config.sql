-- Migration 029: Allow NULL template in call_execution_config for default configs
--
-- Problem: Every template required its own call_execution_config row, even when
-- all timing/settings were identical.
--
-- Solution: A row with template IS NULL acts as the DEFAULT config for a
-- reseller/merchant. The resolution chain is:
--   1. Exact template_id match
--   2. Exact template name match
--   3. Default config (template IS NULL)  <-- NEW
--
-- Changes:
--   1. Make the `template` column nullable
--   2. Drop the old non-partial unique constraint (doesn't handle NULLs correctly)
--   3. Replace with partial unique indexes that cover all four cases:
--        a) template IS NOT NULL, merchant IS NOT NULL  → unique on (merchant_id, template)
--        b) template IS NOT NULL, merchant IS NULL      → unique on (reseller_id, template)
--        c) template IS NULL,     merchant IS NOT NULL  → unique on (reseller_id, merchant_id)  [DEFAULT per merchant]
--        d) template IS NULL,     merchant IS NULL      → unique on (reseller_id)               [DEFAULT for reseller]

-- Step 1: Make template nullable
ALTER TABLE call_execution_config
    ALTER COLUMN "template" DROP NOT NULL;

-- Step 2: Drop the old non-null-aware unique constraint
ALTER TABLE call_execution_config
    DROP CONSTRAINT IF EXISTS uq_call_execution_config_merchant_template;

-- Step 3a: Per-template, per-merchant uniqueness (matches old behaviour)
CREATE UNIQUE INDEX IF NOT EXISTS uq_call_execution_config_template_merchant
    ON call_execution_config (merchant_id, template)
    WHERE template IS NOT NULL AND merchant_id IS NOT NULL;

-- Step 3b: Per-template, no merchant (reseller-level) uniqueness
-- (replaces the old uq_call_execution_config_reseller_template_null_merchant)
DROP INDEX IF EXISTS uq_call_execution_config_reseller_template_null_merchant;
CREATE UNIQUE INDEX IF NOT EXISTS uq_call_execution_config_template_null_merchant
    ON call_execution_config (reseller_id, template)
    WHERE template IS NOT NULL AND merchant_id IS NULL;

-- Step 3c: Default config per merchant (template IS NULL, merchant IS NOT NULL)
CREATE UNIQUE INDEX IF NOT EXISTS uq_call_execution_config_default_with_merchant
    ON call_execution_config (reseller_id, merchant_id)
    WHERE template IS NULL AND merchant_id IS NOT NULL;

-- Step 3d: Default config for reseller (template IS NULL, merchant IS NULL)
CREATE UNIQUE INDEX IF NOT EXISTS uq_call_execution_config_default_no_merchant
    ON call_execution_config (reseller_id)
    WHERE template IS NULL AND merchant_id IS NULL;
