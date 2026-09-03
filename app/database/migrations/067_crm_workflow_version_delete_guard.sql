-- 067: outreach — crm_workflow_version DELETE guard (ADR 0023 §5 as amended
-- 3 Sep 2026: versions are kept for the life of their plan, never deleted).
--
-- 064 shipped the UPDATE trigger only and said a retention sweep would
-- delete unreferenced old versions. The sweep was dropped before merge:
-- a version row is one small document, a plan publishes tens of them,
-- every read is a point lookup, and an exited run's workflow_version must
-- keep answering "what did this run execute". With one DB role,
-- invariants live in tables (module rules, table self-defense) — a
-- decision kept by discipline is not kept. 064 is merged and immutable,
-- so the guard is its own file. TRUNCATE is not guarded: it is an
-- operator's deliberate act, never a code path.

CREATE OR REPLACE FUNCTION crm_workflow_version_undeletable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'crm_workflow_version rows are never deleted — a version is kept for the life of its plan (ADR 0023 §5)';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER crm_workflow_version_delete_guard
    BEFORE DELETE ON crm_workflow_version
    FOR EACH ROW EXECUTE FUNCTION crm_workflow_version_undeletable();
