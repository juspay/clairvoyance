-- 056: permission — crm_decision_log (T14, canon/03-permission.md)
--
-- One row per automated verdict, allow and refuse alike. Nothing can rebuild
-- this table later: the inputs a decision saw are gone by the time anyone asks
-- about it, so the reasoning is written as the decision is made or not at all.
--
-- `chosen` is the decider's working state dumped as-is — no schema beyond the
-- required verdict, because the only reader is a person on an audit day.
-- customer_id carries no FK on purpose: an audit record has to survive the
-- customer row being erased or merged away.
--
-- jsonb_typeof before the key test, because `?` on an array asks whether the
-- array CONTAINS that string: '["verdict"]' passes `chosen ? 'verdict'` while
-- having no verdict field at all, and the audit read would find nothing there.
CREATE TABLE crm_decision_log (
    id            bigserial PRIMARY KEY,
    merchant_id   text NOT NULL,
    customer_id   uuid,
    decision_kind text NOT NULL
                  CHECK (decision_kind IN ('send_or_hold', 'identity_merge')),
    chosen        jsonb NOT NULL
                  CHECK (jsonb_typeof(chosen) = 'object' AND chosen ? 'verdict'),
    decided_at    timestamptz NOT NULL DEFAULT now()
);

-- The why-didn't-it-send read: one customer's verdicts, newest first.
CREATE INDEX crm_decision_log_merchant_customer_ix
    ON crm_decision_log (merchant_id, customer_id, decided_at DESC);

-- Same append-only rule as the consent ledger, for the same reason: this table
-- is the only account of why a message was sent or held, and the inputs that
-- produced the verdict are gone by the time anyone asks. A row that can be
-- edited afterwards is not an audit record — it is a claim about one.
--
-- Row triggers, so TRUNCATE and partition/table drops still pass. Retention on
-- a table this size is a bulk operation, not a DELETE per verdict, and pruning
-- old rows wholesale is a different act from rewriting one.
CREATE OR REPLACE FUNCTION crm_decision_log_append_only()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'crm_decision_log is append-only: % refused. A verdict is what the '
        'gate decided at that moment; a later one is a new row.', TG_OP;
END;
$$;

CREATE TRIGGER crm_decision_log_no_update
    BEFORE UPDATE ON crm_decision_log
    FOR EACH ROW EXECUTE FUNCTION crm_decision_log_append_only();

CREATE TRIGGER crm_decision_log_no_delete
    BEFORE DELETE ON crm_decision_log
    FOR EACH ROW EXECUTE FUNCTION crm_decision_log_append_only();
