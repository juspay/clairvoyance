ALTER TABLE evaluation_result
    DROP CONSTRAINT evaluation_result_state_check;

ALTER TABLE evaluation_result
    ADD CONSTRAINT evaluation_result_state_check
        CHECK (
            status = 'COMPLETED'
            OR (
                status = 'PROCESSING'
                AND evaluation_type = 'GUARDRAIL'
                AND metadata IS NOT NULL
            )
            OR (
                status IN ('PROCESSING', 'FAILED')
                AND metadata IS NULL
            )
        );
