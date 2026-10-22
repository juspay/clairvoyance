-- Migration: exactly one stable door-name per merchant-connector door, so a
-- plan can address a door by a name the URL change does not move.
-- Description: crm_connector_installation is the door a merchant holds on one
-- connector. An action node names WHICH door it acts through. It used to name
-- the row's primary key (installation_id), so changing the door's base URL
-- forced a NEW row and a NEW id — every plan was republished and pinned runs
-- were parked by the old id.
--
-- This adds `door_name` — the stable, merchant-chosen name a plan addresses
-- (`payments`, `returns`) — and `door_data`, a jsonb bag for connector-shaped
-- address data. merchant_http moves its base URL OUT of external_account_id
-- (the provider-id column, and NOT NULL + uniquely keyed) into
-- door_data.base_url, so editing a URL is an UPDATE to that one row: same
-- door_name, same id, no republish, pinned runs untouched. For every other
-- connector external_account_id keeps meaning "the provider's id", and
-- door_name backfills to it (an operator can rename later).
--
-- door_name is unique per (merchant, connector) where present, and every
-- merchant_http door gets one at onboarding (a merchant holding two base_urls
-- names each). It is NULL only for doors never re-onboarded after this
-- migration — those fall back to the newest-usable-door rule.

ALTER TABLE crm_connector_installation ADD COLUMN door_name text;
ALTER TABLE crm_connector_installation ADD COLUMN door_data jsonb NOT NULL DEFAULT '{}'::jsonb;

-- Every existing door's name starts as its provider account id, the only
-- stable string a plan already could have meant. merchant_http doors keep
-- pointing at their base_url in door_data, so a later rename is one UPDATE to
-- door_name (the plan already reads it, so no republish).
UPDATE crm_connector_installation
   SET door_name = external_account_id;

CREATE UNIQUE INDEX crm_connector_installation_name_uq
    ON crm_connector_installation (merchant_id, connector_key, door_name)
    WHERE door_name IS NOT NULL;