-- Add index for call_id lookups on lead_call_tracker.
-- GCP Query Insights shows `SELECT * FROM "lead_call_tracker" WHERE "call_id" = $1`
-- as the top query by CPU load with ~1,775ms avg execution time.
-- Full table scan occurs because no index exists on call_id.
CREATE INDEX IF NOT EXISTS idx_lead_call_tracker_call_id
    ON lead_call_tracker (call_id);
