-- Migration 022: Add performance indexes for analytics queries
-- Description:
--   1. Add single-column indexes for basic lookups
--   2. Add composite indexes for common analytics query patterns
--
-- Background:
--   Analytics queries filter by call_initiated_time but had no index,
--   causing full table scans on large datasets.
--
-- Query patterns covered by composite indexes:
--   Pattern 1: execution_mode + outcome + date
--   Pattern 2: execution_mode + merchant + date
--   Pattern 3: execution_mode + merchant + shop + date
--   Pattern 4: execution_mode + merchant + shop + outcome + date
--
-- Note: PostgreSQL can use a prefix of composite indexes, so the 5-column
-- index can serve patterns 2, 3, and 4.

BEGIN;

-- =====================================================
-- Single-column indexes (basic lookups)
-- =====================================================

-- Index on call_initiated_time (primary date filter in all analytics queries)
CREATE INDEX IF NOT EXISTS idx_lead_call_tracker_call_initiated_time
    ON lead_call_tracker (call_initiated_time);

-- Index on call_end_time (used for duration calculations and end-time filtering)
CREATE INDEX IF NOT EXISTS idx_lead_call_tracker_call_end_time
    ON lead_call_tracker (call_end_time);

-- Index on updated_at (commonly queried for recent changes and sync operations)
CREATE INDEX IF NOT EXISTS idx_lead_call_tracker_updated_at
    ON lead_call_tracker (updated_at);

-- Index on outbound_number_id (JOIN with outbound_number table for provider info)
CREATE INDEX IF NOT EXISTS idx_lead_call_tracker_outbound_number_id
    ON lead_call_tracker (outbound_number_id);

-- =====================================================
-- Composite indexes for analytics query patterns
-- =====================================================

-- Pattern 1: execution_mode + outcome + date
-- Covers queries filtering by outcome within a date range
-- Example: Get all CONFIRM outcomes for March 2026
CREATE INDEX IF NOT EXISTS idx_lct_exec_outcome_date
    ON lead_call_tracker (execution_mode, outcome, call_initiated_time);

-- Patterns 2, 3, 4: execution_mode + merchant + shop + outcome + date
-- Covers multiple patterns (PostgreSQL can use index prefix):
--   - Pattern 2: Filter by merchant + date
--   - Pattern 3: Filter by merchant + shop + date
--   - Pattern 4: Filter by merchant + shop + outcome + date
-- Example: Get CONFIRM outcomes for Merchant X's Shop Y in March 2026
CREATE INDEX IF NOT EXISTS idx_lct_exec_merchant_shop_outcome_date
    ON lead_call_tracker (
        execution_mode,
        reseller_id,
        merchant_id,
        outcome,
        call_initiated_time
    );

COMMIT;
