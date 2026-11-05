ALTER TABLE evaluation_config
    ADD COLUMN id uuid PRIMARY KEY DEFAULT gen_random_uuid();

ALTER TABLE topic_result RENAME TO evaluation_result;

ALTER TABLE evaluation_result
    RENAME COLUMN topic_type TO result;

ALTER TABLE evaluation_result
    RENAME COLUMN topic TO metadata;

ALTER TABLE evaluation_result
    ADD COLUMN evaluation_type evaluation_type NOT NULL DEFAULT 'TOPIC',
    ADD COLUMN evaluation_config_id uuid;

UPDATE evaluation_result AS evaluation
SET evaluation_config_id = config.id
FROM evaluation_config AS config
WHERE config.template_id = evaluation.template_id
  AND config.evaluation_type = evaluation.evaluation_type;

ALTER TABLE evaluation_result
    ALTER COLUMN evaluation_config_id SET NOT NULL,
    ADD CONSTRAINT evaluation_result_evaluation_config_id_fkey
        FOREIGN KEY (evaluation_config_id) REFERENCES evaluation_config(id);

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
    TO evaluation_result_metadata_check;

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

CREATE UNIQUE INDEX evaluation_result_source_result_unique
    ON evaluation_result (source_id, evaluation_type, result)
    WHERE result IS NOT NULL;

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
