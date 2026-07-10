-- Migration 036: Attach data-source usage config to Breeze Buddy templates.
--
-- The JSONB stores TemplateDataSourceRef[] using the template data-source model:
-- data_source reference + datasets[] with target/selector/format/variable_name.
-- Source contents stay out of Postgres template rows;
-- runtime fetches are cached separately in Redis.

ALTER TABLE template
    ADD COLUMN IF NOT EXISTS data_sources jsonb;
