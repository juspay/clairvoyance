-- Migration: Add is_approved column to template table
-- Description:
--   1. Add is_approved column to template table with default false

ALTER TABLE template
ADD COLUMN IF NOT EXISTS is_approved BOOLEAN DEFAULT false;
