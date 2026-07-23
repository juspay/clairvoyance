-- Migration: Add telephony number lookup index
-- Description: number has had zero index coverage since migration 009 dropped
-- the original UNIQUE(number) constraint (and its backing index) -- every
-- lookup by number, including inbound call routing (get_telephony_number_by_number,
-- every inbound call) and the buy flow's duplicate pre-check
-- (check_number_purchase_conflict), has been a sequential scan since.
--
-- Deliberately NOT partial/unique: inbound call routing must resolve a
-- number regardless of status (a DISABLED number still needs to route/reject
-- correctly), and the duplicate pre-check must see DISABLED rows too, to
-- decide whether a re-buy is allowed. A partial index can't serve a query
-- that isn't proven to exclude the rows it omits. Enforces nothing at the DB
-- level -- duplicate prevention for the buy flow is handled at the
-- application level (RedisLock + check_number_purchase_conflict); Plivo
-- itself also rejects buying an already-rented number.
--
-- Targets telephony_numbers (renamed from outbound_number in migration 038) --
-- the old name is now only a compatibility VIEW and cannot carry an index.
CREATE INDEX IF NOT EXISTS idx_telephony_numbers_number
    ON telephony_numbers (number);
