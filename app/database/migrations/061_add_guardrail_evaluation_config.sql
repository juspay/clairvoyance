ALTER TABLE evaluation_config
    DROP CONSTRAINT evaluation_config_runtime_check;

ALTER TABLE evaluation_config
    ADD CONSTRAINT evaluation_config_runtime_check
        CHECK (
            evaluation_type = 'GUARDRAIL'
            OR (
                evaluation_type = 'TOPIC'
                AND jsonb_typeof(configuration -> 'model') = 'string'
                AND btrim(COALESCE(configuration ->> 'model', '')) <> ''
                AND jsonb_typeof(configuration -> 'system_prompt') = 'string'
                AND btrim(COALESCE(configuration ->> 'system_prompt', '')) <> ''
            )
        );
