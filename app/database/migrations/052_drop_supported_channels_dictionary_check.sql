-- Migration 048: Stop encoding the channel dictionary in a CHECK constraint.
--
-- Migration 027 added:
--
--     CHECK (cardinality(supported_channels) >= 1
--            AND supported_channels <@ ARRAY['voice', 'chat'])
--
-- The second half hard-codes the set of known channels into the schema, so
-- adding a channel (WhatsApp, RCS, …) requires a migration before a single
-- row can name it. That coupling is the reason a new channel cannot be
-- introduced today, and it is the constraint this migration removes.
--
-- The channel dictionary now lives in code only. The DB stores whatever the
-- application says is valid; validating the vocabulary is the application's
-- job, where adding a channel is a code change rather than a schema change.
--
-- The cardinality half is a different kind of rule — it is not about *which*
-- channels exist but about a template being servable on at least one. An
-- empty array would leave a template silently unroutable, so that guarantee
-- is re-added on its own. (``cardinality`` rather than ``array_length``:
-- the latter returns NULL for '{}', and a NULL CHECK predicate passes,
-- which would let the empty array through — see migration 027.)

ALTER TABLE template
    DROP CONSTRAINT IF EXISTS template_supported_channels_check;

ALTER TABLE template
    ADD CONSTRAINT template_supported_channels_non_empty CHECK (
        cardinality(supported_channels) >= 1
    );
