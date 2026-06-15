-- Migration 034: Add data_sources column to template table
-- Stores an array of data source references attached to this template.
-- Each element: {"data_source_id": "uuid", "name": "var_name", "inject_as": "var"|"message"}

ALTER TABLE template
    ADD COLUMN IF NOT EXISTS data_sources JSONB;

COMMENT ON COLUMN template.data_sources IS
    'Array of data source refs: [{data_source_id, name, inject_as}]. '
    'name becomes the {placeholder} in template vars. '
    'inject_as: "var" = render into prompts, "message" = prepend system message.';
