-- Migration: Add template_id columns for UUID-based template references
-- Description:
--   1. Add template_id (UUID) column to lead_call_tracker table
--   2. Add template_id (UUID) column to call_execution_config table
--   3. Add performance indexes
--   4. Add foreign key constraints for referential integrity
--
-- Background:
--   Currently template is referenced by name (VARCHAR) which can:
--   - Collide across merchants (non-unique names)
--   - Break historical data when templates are renamed
--   - Prevent proper referential integrity
--
--   This migration adds template_id alongside existing template name column
--   for backward compatibility during transition period.

-- Add template_id column to lead_call_tracker
ALTER TABLE lead_call_tracker
ADD COLUMN IF NOT EXISTS template_id UUID;

-- Add template_id column to call_execution_config
ALTER TABLE call_execution_config
ADD COLUMN IF NOT EXISTS template_id UUID;

-- Add indexes for performance
CREATE INDEX IF NOT EXISTS idx_lead_call_tracker_template_id
    ON lead_call_tracker(template_id);

CREATE INDEX IF NOT EXISTS idx_call_execution_config_template_id
    ON call_execution_config(template_id);

-- Add foreign key constraints for referential integrity
-- ON DELETE SET NULL: If template is deleted, set template_id to NULL in lead_call_tracker
-- (historical data is preserved with template name, but link to template table is broken)
ALTER TABLE lead_call_tracker
ADD CONSTRAINT fk_lead_call_tracker_template_id
FOREIGN KEY (template_id) REFERENCES template(id) ON DELETE SET NULL;

-- ON DELETE RESTRICT: Prevent deletion of templates that are referenced in call_execution_config
-- (configs actively depend on templates, so template deletion should be blocked)
ALTER TABLE call_execution_config
ADD CONSTRAINT fk_call_execution_config_template_id
FOREIGN KEY (template_id) REFERENCES template(id) ON DELETE RESTRICT;
