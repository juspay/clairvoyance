-- Observer configs are shaped { "observers": [...] }, not the { model,
-- system_prompt } that 044 wrote the check for, so the TOPIC-only constraint
-- rejects them. Widen it per evaluation_type.
--
-- Separate from 046 on purpose: Postgres refuses to use an enum value in the
-- same transaction that added it, and the runner wraps each file in its own
-- transaction. Merging these two files makes the migration fail.
ALTER TABLE evaluation_config DROP CONSTRAINT evaluation_config_runtime_check;

ALTER TABLE evaluation_config
    ADD CONSTRAINT evaluation_config_runtime_check
    CHECK (
        (
            evaluation_type = 'TOPIC'
            AND jsonb_typeof(configuration -> 'model') = 'string'
            AND btrim(COALESCE(configuration ->> 'model', '')) <> ''
            AND jsonb_typeof(configuration -> 'system_prompt') = 'string'
            AND btrim(COALESCE(configuration ->> 'system_prompt', '')) <> ''
        )
        OR (
            evaluation_type = 'OBSERVER'
            AND jsonb_typeof(configuration -> 'observers') = 'array'
        )
    );
