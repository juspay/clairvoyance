-- 087: outreach — crm_campaign (canon T22, canon/06-outreach.md). Broadcasts
-- phase 1: the badge registry a broadcast wears. Named crm_campaign, not
-- campaign — buddy's bulk-lead `campaign` table already holds that name
-- (docs/crm/migrations.md).
--
-- A registry that NAMES a season, never contains it: nothing points from a
-- campaign to anything; executions wear the key. The composite FK from
-- crm_broadcast (088) turns a tag typo into an insert error instead of one
-- season's report split across two spellings of Diwali.

CREATE TABLE crm_campaign (
    -- Tenancy — half the key.
    merchant_id   text NOT NULL,
    -- The stamp executions carry (diwali-2026): chosen once, stable forever,
    -- so a slug — lowercase letters and digits, single inner hyphens.
    key           text NOT NULL
                  CHECK (key ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND length(key) <= 64),
    -- The display name: renameable without touching one stamped row (key is
    -- identity, name is presentation). Never blank, never untrimmed.
    name          text NOT NULL CHECK (name <> '' AND name !~ '^\s|\s$'),
    -- The report window's header — for eyes, never a gate: a campaign never
    -- blocks a send.
    starts_at     date,
    ends_at       date,
    created_by    text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    -- merchant_id first (tenancy law); the key is unique per merchant.
    PRIMARY KEY (merchant_id, key),
    CHECK (starts_at IS NULL OR ends_at IS NULL OR ends_at >= starts_at)
);
