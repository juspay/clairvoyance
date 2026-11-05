-- Migration 034: Store per-merchant S2S token on the merchants table
-- The token minted by POST /merchant (issue_token=true) is stored here (nullable)
-- and used as the platform webhook's HMAC secret (e.g. WooCommerce's
-- X-WC-Webhook-Signature). NULL until a token is issued for the merchant.

BEGIN;

ALTER TABLE merchants ADD COLUMN IF NOT EXISTS s2s_token TEXT;

COMMIT;
