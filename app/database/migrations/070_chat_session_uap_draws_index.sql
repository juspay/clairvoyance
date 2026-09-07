-- 070: the ticket-payment ledger lives on chat sessions
-- (metadata.uap_draws, one element per Juspay order). A rider's usage
-- against one agent is summed over every session of that rider, found by
-- the rider_ref template var (the CRM customer id; chat_session.customer_id
-- is the legacy customers FK and cannot hold it). Only sessions that carry
-- draws are indexed, so the index stays tiny.
CREATE INDEX IF NOT EXISTS idx_chat_session_uap_rider
    ON chat_session ((metadata -> 'template_vars' ->> 'rider_ref'))
    WHERE metadata ? 'uap_draws';
