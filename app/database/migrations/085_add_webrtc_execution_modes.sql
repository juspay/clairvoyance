-- Migration: Add WEBRTC and WEBRTC_TEST to execution_mode CHECK constraint
-- Description: Enables the SmallWebRTC transport (serverless P2P WebRTC for
--              device/embedded and browser-test clients). WEBRTC is production
--              device traffic; WEBRTC_TEST is the loom Test dialog. Both are
--              client-initiated (like DAILY/DAILY_TEST) and excluded from cron
--              dispatch pickup.

-- Drop old constraint and re-create with WEBRTC + WEBRTC_TEST included.
ALTER TABLE lead_call_tracker
DROP CONSTRAINT IF EXISTS lead_call_tracker_execution_mode_check;

-- NOT VALID: skip the full-table scan under ACCESS EXCLUSIVE — new/updated
-- rows are checked immediately, existing rows are verified by the separate
-- VALIDATE below, which only takes SHARE UPDATE EXCLUSIVE (reads and writes
-- keep flowing on a large lead_call_tracker).
ALTER TABLE lead_call_tracker
ADD CONSTRAINT lead_call_tracker_execution_mode_check
CHECK (execution_mode IN ('TELEPHONY', 'TELEPHONY_TEST', 'DAILY', 'DAILY_TEST', 'DAILY_STREAM', 'HOLD_TRANSFER', 'WEBRTC', 'WEBRTC_TEST'))
NOT VALID;

ALTER TABLE lead_call_tracker
VALIDATE CONSTRAINT lead_call_tracker_execution_mode_check;
