-- Migration: Add CONVERSATION_EVALS to the evaluation_type enum
-- Description: CONVERSATION_EVALS is the finished-call judgment family
-- (adapter + pluggable engines; the first engine is TypeSafe Jev). No rows
-- are seeded: an agent's row is created by the admin configuration POST
-- (born disabled) and an agent without an enabled evaluation_config row
-- does not run the evaluation.
--
-- The runtime CHECK is shape-based, not type-based: a configuration must
-- match one of the two known shapes — the prompt-driven shape (non-empty
-- model + system_prompt, TOPIC's original rule, unchanged) or the typed
-- judgment shape (non-empty engine + provider + model, and a questions
-- object). Which shape belongs to which evaluation_type is the API
-- validators' job (each write path validates its own type in code); the
-- constraint guarantees no row can hold neither shape. It references no
-- enum values at all, so it shares this transaction with the enum
-- addition without restriction.

ALTER TYPE evaluation_type ADD VALUE IF NOT EXISTS 'CONVERSATION_EVALS';

ALTER TABLE evaluation_config
    DROP CONSTRAINT evaluation_config_runtime_check;

ALTER TABLE evaluation_config
    ADD CONSTRAINT evaluation_config_runtime_check
        CHECK (
            (
                jsonb_typeof(configuration -> 'model') = 'string'
                AND btrim(COALESCE(configuration ->> 'model', '')) <> ''
                AND jsonb_typeof(configuration -> 'system_prompt') = 'string'
                AND btrim(COALESCE(configuration ->> 'system_prompt', '')) <> ''
            )
            OR (
                jsonb_typeof(configuration -> 'engine') = 'string'
                AND btrim(COALESCE(configuration ->> 'engine', '')) <> ''
                AND jsonb_typeof(configuration -> 'provider') = 'string'
                AND btrim(COALESCE(configuration ->> 'provider', '')) <> ''
                AND jsonb_typeof(configuration -> 'model') = 'string'
                AND btrim(COALESCE(configuration ->> 'model', '')) <> ''
                AND jsonb_typeof(configuration -> 'questions') = 'object'
            )
        );
