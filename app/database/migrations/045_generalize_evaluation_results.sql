ALTER TABLE topic_result RENAME TO evaluation_result;

ALTER TABLE evaluation_result
    RENAME COLUMN topic_type TO result_type;

ALTER TABLE evaluation_result
    RENAME COLUMN topic TO result;

ALTER TABLE evaluation_result
    ADD COLUMN evaluation_type evaluation_type NOT NULL DEFAULT 'TOPIC';

ALTER TABLE evaluation_result
    RENAME CONSTRAINT topic_result_pkey TO evaluation_result_pkey;

ALTER TABLE evaluation_result
    RENAME CONSTRAINT topic_result_template_id_fkey
    TO evaluation_result_template_id_fkey;

ALTER TABLE evaluation_result
    RENAME CONSTRAINT topic_result_status_check
    TO evaluation_result_status_check;

ALTER TABLE evaluation_result
    RENAME CONSTRAINT topic_result_json_check
    TO evaluation_result_json_check;

ALTER TABLE evaluation_result
    RENAME CONSTRAINT topic_result_identity_check
    TO evaluation_result_identity_check;

ALTER TABLE evaluation_result
    RENAME CONSTRAINT topic_result_state_check
    TO evaluation_result_state_check;

DROP INDEX topic_result_source_topic_unique;
DROP INDEX topic_result_template_time;
DROP INDEX topic_result_tenant_time;
DROP INDEX topic_result_completed_time;

CREATE UNIQUE INDEX evaluation_result_source_type_unique
    ON evaluation_result (source_id, evaluation_type, result_type)
    WHERE result_type IS NOT NULL;

CREATE INDEX evaluation_result_template_time
    ON evaluation_result (
        template_id, evaluation_type, started_at DESC, id DESC
    )
    WHERE status = 'COMPLETED';

CREATE INDEX evaluation_result_tenant_time
    ON evaluation_result (
        reseller_id, merchant_id, evaluation_type, started_at DESC, id DESC
    )
    WHERE status = 'COMPLETED';

CREATE INDEX evaluation_result_completed_time
    ON evaluation_result (evaluation_type, started_at DESC, id DESC)
    WHERE status = 'COMPLETED';
