-- Migration: make telephony_numbers capacity columns explicit
-- Description: capacity is read through COALESCE("maximum_channels", 0), so a
-- NULL in that column is not "unlimited" and not "unset" -- it is a hard
-- ceiling of ZERO. Before inbound was gated that only affected outbound
-- (Plivo/Exotel could never acquire such a number, which looks like a routing
-- problem and gets worked around). Now it also turns away real inbound callers
-- with a busy message, on a number whose only sin is that nobody ever typed a
-- channel count.
--
-- The system already has an opinion about what NULL means: the channel-token
-- reconciler reads `n.maximum_channels if n.maximum_channels is not None else 1`
-- (dispatch/reconcilers.py). This migration makes that opinion the stored
-- truth instead of a per-reader fallback, so the SQL ceiling and the Python
-- ceiling can no longer disagree.
--
-- Backfill value is 1, matching that reconciler. Note the buy flow's schema
-- (TelephonyNumberBuyRequest) defaults to 10, but that is a deliberate choice
-- made when provisioning a number; admin create leaves the field unset and now
-- inherits this column's DEFAULT instead. The backfill is neither of those: a row that reached here with NULL was never deliberately
-- provisioned for concurrency, and quietly granting it ten simultaneous calls
-- would over-commit a trunk nobody sized. One channel is the conservative
-- floor -- the number works, and raising it is an explicit operator decision.
--
-- channels is given the same treatment for the same reason: every reader
-- already COALESCEs it to 0, so storing 0 changes no behaviour and removes a
-- second three-valued column from the capacity math.

-- SIDE EFFECT, read before applying: a NULL ceiling blocked OUTBOUND too,
-- because _acquire_number goes through the same COALESCE(maximum_channels, 0)
-- and could never satisfy `channels < 0`. Backfilling to 1 therefore returns
-- these numbers to the dialable pool as well as unblocking inbound. That is
-- the correct state — a number with no channel count was misconfigured, not
-- deliberately disabled (a deliberate zero is stored as 0 and preserved here)
-- — but it is a behaviour change for outbound, not only a fix for inbound.
-- Check what you are about to re-enable first:
--   SELECT count(*), status, provider FROM telephony_numbers
--   WHERE maximum_channels IS NULL GROUP BY status, provider;
--
-- DEPLOY ORDER: ship the CODE FIRST, then run this migration. The previous
-- insert_telephony_number_query always bound maximum_channels as a parameter,
-- and CreateTelephonyNumberRequest leaves it None — so while old pods are
-- still serving against a migrated database, admin create-number raises
-- NotNullViolation and surfaces as a 400. The reverse order is strictly safe:
-- the new query simply omits a still-nullable column, which reproduces the old
-- NULL behaviour exactly.
--
-- DEPLOY NOTE: scripts/migrate.py runs each migration inside one transaction,
-- so the ACCESS EXCLUSIVE lock taken by the first ALTER is held until the
-- final CHECK finishes validating. Both CHECKs scan the table, and every live
-- call on these numbers takes a row UPDATE on it, so those block for the
-- duration. telephony_numbers is small (tens of rows), so this is a moment,
-- not an outage — but apply it during a low-call window rather than mid-peak.

UPDATE telephony_numbers SET maximum_channels = 1 WHERE maximum_channels IS NULL;
UPDATE telephony_numbers SET channels = 0 WHERE channels IS NULL;

-- Clamp negatives BEFORE the CHECKs below, or the ALTER fails and takes the
-- whole migration with it. GREATEST(0, ...) guards the decrement today, but
-- rows predating that guard can still hold a negative count, and a migration
-- that aborts on deploy is a worse outcome than the drift it is cleaning up.
UPDATE telephony_numbers SET channels = 0 WHERE channels < 0;
-- A negative ceiling is corrupt, not a deliberate zero, so it lands on the
-- same conservative 1 as a NULL. Clamping it to 0 would leave the number
-- silently unable to carry any call at all.
UPDATE telephony_numbers SET maximum_channels = 1 WHERE maximum_channels < 0;

ALTER TABLE telephony_numbers ALTER COLUMN maximum_channels SET DEFAULT 1;
ALTER TABLE telephony_numbers ALTER COLUMN maximum_channels SET NOT NULL;

ALTER TABLE telephony_numbers ALTER COLUMN channels SET DEFAULT 0;
ALTER TABLE telephony_numbers ALTER COLUMN channels SET NOT NULL;

-- Format CHECKs (canon: CHECKs on FORMAT are required; vocabulary stays in
-- code). A negative ceiling or a negative in-use count is never a legitimate
-- state, and the decrement's GREATEST(0, ...) already assumes it cannot happen.
-- Drop-then-add so the file stays re-runnable, matching 071's pattern. The
-- ALTER COLUMN statements above are already idempotent; ADD CONSTRAINT is not.
ALTER TABLE telephony_numbers
    DROP CONSTRAINT IF EXISTS telephony_numbers_channels_non_negative;
ALTER TABLE telephony_numbers
    ADD CONSTRAINT telephony_numbers_channels_non_negative
    CHECK ("channels" >= 0);

ALTER TABLE telephony_numbers
    DROP CONSTRAINT IF EXISTS telephony_numbers_maximum_channels_non_negative;
ALTER TABLE telephony_numbers
    ADD CONSTRAINT telephony_numbers_maximum_channels_non_negative
    CHECK ("maximum_channels" >= 0);
