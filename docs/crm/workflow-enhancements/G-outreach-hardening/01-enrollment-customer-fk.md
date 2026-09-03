# G/01 — Tenancy FK on crm_workflow_enrollment.customer_id (P4)

**Track G · step 1** · **Kind**: migration · **PR title**: `fix(crm): pin enrollment customers to the same tenant with a composite FK (enh G/01)` · **Depends on**: nothing · **Notes**: §11 P4, migration 056's precedent

## Design
- Migration `NNN_add_enrollment_customer_fk.sql`: `ALTER TABLE crm_workflow_enrollment ADD CONSTRAINT crm_workflow_enrollment_customer_fk FOREIGN KEY (merchant_id, customer_id) REFERENCES crm_customer (merchant_id, id) NOT VALID; ALTER TABLE … VALIDATE CONSTRAINT …;` — `NOT VALID` then `VALIDATE` so the lock is short on a live table. Header cites T20 and why (a wrong merchant_id would file a run against another tenant's customer — the same argument 056 made for crm_message).
- Pre-flight in the migration (a `DO` block that raises with the count if any orphan rows exist) so the migration fails loudly rather than half-applying; the runbook says how to find and exit orphans first.
- track B's `crm_handoff` already carries this FK; this brings enrollment in line.

## Red tests
- Migration numbering guard; nothing else executable without a DB — state in the PR that it was applied on a scratch DB.
