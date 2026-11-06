-- 063: crm_workflow_enrollment.exit_reason gains 'converted_elsewhere'
-- (canon T20 col exit_reason; rollout phase 06 — goal tiers with a key).
--
-- A goal is now a list of TIERS, each {topics, key?, exit_reason}. For
-- cart recovery: an order carrying the run's own cart token ends the run
-- as goal_met ("THIS cart recovered"); any other order by the customer
-- still ends it — never nudge someone who just bought — but as
-- converted_elsewhere, so the funnel can tell the two apart. The reason
-- vocabulary is a closed status enum, so the CHECK is required (law 11)
-- and amended here rather than dropped.
--
-- 058 declared the CHECK inline on the column; Postgres named it
-- crm_workflow_enrollment_exit_reason_check (verified on 16.10). The
-- replacement carries an explicit name so the next amendment never has
-- to guess. Canon amendment (the sixth value) proposed to Swaroop with
-- this phase, beside 058's 'completed'.

ALTER TABLE crm_workflow_enrollment
    DROP CONSTRAINT crm_workflow_enrollment_exit_reason_check;

ALTER TABLE crm_workflow_enrollment
    ADD CONSTRAINT crm_workflow_enrollment_exit_reason_ck
    CHECK (exit_reason IN ('goal_met', 'timed_out', 'withdrawn',
                           'ejected', 'completed', 'converted_elsewhere'));
