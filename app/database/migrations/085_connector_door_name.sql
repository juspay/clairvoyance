-- Migration: connector-shaped address data on a merchant's connector account,
-- so editing an address is an in-place UPDATE that no plan notices.
-- Description: crm_connector_installation is the account a merchant holds on
-- one connector. An action node selects it by external_account_id — the
-- existing (merchant_id, connector_key, external_account_id) unique key —
-- never by the row id or an address.
--
-- This adds `door_data`, a jsonb bag for address data that is NOT identity.
-- merchant_http stores its stable, merchant-chosen account name in
-- external_account_id and its base URL in door_data.base_url (written at
-- onboarding). Re-onboarding with a new URL conflicts on the same unique key
-- and updates door_data on the same row: same external_account_id, same id,
-- no republish, pinned runs untouched. Every other connector leaves it '{}'.

ALTER TABLE crm_connector_installation ADD COLUMN door_data jsonb NOT NULL DEFAULT '{}'::jsonb;
