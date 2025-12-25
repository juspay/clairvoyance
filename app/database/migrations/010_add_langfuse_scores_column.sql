-- Migration: Add langfuse_scores column to lead_call_tracker
-- Description: Store Langfuse evaluation scores for each call

-- Add langfuse_scores column to store evaluation results
ALTER TABLE lead_call_tracker 
ADD COLUMN IF NOT EXISTS langfuse_scores JSONB DEFAULT NULL;

-- Add index for querying records with/without scores
CREATE INDEX IF NOT EXISTS idx_lead_call_tracker_langfuse_scores_null 
    ON lead_call_tracker ((langfuse_scores IS NULL));

-- Example structure of langfuse_scores:
-- {
--   "trace_id": "488464e5170c95c6ac770f158a73e48e",
--   "trace_url": "https://periscope.breeze.in/trace/488464e5...",
--   "scores": [
--     {
--       "id": "ed32b7e7-989b-4ead-afbe-33f2b1fdc9ed",
--       "name": "HIGH LATENCY",
--       "value": 1,
--       "comment": "No latency issues detected",
--       "timestamp": "2025-12-24T10:59:26.396Z"
--     },
--     {
--       "id": "5c0fe7a6-c163-4000-9da1-7ca9aa767f8a",
--       "name": "OUTCOME MISMATCH",
--       "value": 1,
--       "comment": "Outcome matches user intent",
--       "timestamp": "2025-12-24T10:59:21.079Z"
--     }
--   ],
--   "fetched_at": "2025-12-24T10:59:33Z"
-- }
