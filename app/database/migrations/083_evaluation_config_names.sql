-- Migration: Name evaluation configs so an agent can have many evals, and seed
-- the built-in outcome_correctness eval
-- Description: An agent (template) had at most one row per evaluation type.
-- Every row now has a name, unique per agent, so an agent can carry many
-- CONVERSATION_EVALS rows (the merchant's evals; the API to create them comes
-- later). TOPIC stays one per agent: its row is always named 'topic'.
-- Existing rows take their type's name ('topic', 'conversation_evals'), which
-- is also the row the per-type endpoints keep addressing.
--
-- The global rows (template_id NULL) are now unique by name rather than by
-- type, so a built-in eval can sit beside the TOPIC default. The built-in
-- outcome_correctness eval is seeded here: it runs right after a telephony
-- call, before call.completed, and may correct the call's outcome
-- (conversation_analysis/preset/outcome_eval.py). Its questions are built
-- per call from the agent's own outcome words, so the row holds none. The
-- seeded row is the default for every agent, OFF: an agent's own row named
-- 'outcome_correctness' overrides it either way (enabled to turn it on for
-- that agent, disabled to keep it off), and enabling the seeded row turns it
-- on for every agent without a row of its own.
--
-- Results need no index change: each eval stores its result under its own
-- name (evaluation_result.result), so (source_id, evaluation_type, result)
-- stays unique per call per eval.

ALTER TABLE evaluation_config ADD COLUMN name varchar(64);

UPDATE evaluation_config SET name = lower(evaluation_type::text);

ALTER TABLE evaluation_config
    ALTER COLUMN name SET NOT NULL,
    ADD CONSTRAINT evaluation_config_name_format_check
        CHECK (name ~ '^[a-z][a-z0-9_]{0,63}$'),
    ADD CONSTRAINT evaluation_config_topic_name_check
        CHECK (evaluation_type <> 'TOPIC' OR name = 'topic');

ALTER TABLE evaluation_config
    DROP CONSTRAINT evaluation_config_template_type_unique;

ALTER TABLE evaluation_config
    ADD CONSTRAINT evaluation_config_template_name_unique
        UNIQUE (template_id, name);

DROP INDEX evaluation_config_global_default_unique;

CREATE UNIQUE INDEX evaluation_config_global_name_unique
    ON evaluation_config (name)
    WHERE template_id IS NULL;

-- min_confidence: 0.8 for now, to be finalised.
INSERT INTO evaluation_config (evaluation_type, name, enabled, configuration)
VALUES (
    'CONVERSATION_EVALS',
    'outcome_correctness',
    false,
    jsonb_build_object(
        'engine', 'structured',
        'provider', 'typesafe',
        'model', 'jev-1.13.0',
        'questions', '[]'::jsonb,
        'min_confidence', 0.8
    )
);
