-- ═══════════════════════════════════════════════════════════════════════
-- ORPHAN MERCHANTS (read-only) — onboarded but never given a template
--
-- The onboarding service (notclass) creates merchants rows but no
-- template, so these merchants silently fall back to their reseller's
-- shared templates. This report lists them newest-first with the shared
-- templates they're implicitly riding and their recent lead traffic —
-- it is the seed of the provisioning reconciler and stays useful after
-- the backfill (any row appearing here = onboarding gap).
--
-- Run:  DAYS=30 ./scripts/db_readonly_report.sh orphan_merchants.sql
-- Optionally filter one reseller:
--       PSQL_ARGS="-v reseller=BB_SHOPIFY" ... (then uncomment the filter)
-- ═══════════════════════════════════════════════════════════════════════
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
\pset pager off
\timing off

WITH own AS (            -- merchants that own at least one template
  SELECT DISTINCT reseller_id, merchant_id
  FROM template
  WHERE merchant_id IS NOT NULL
), shared_names AS (     -- what each reseller's shared catalog offers
  SELECT reseller_id, string_agg(name, ', ' ORDER BY name) AS shared_templates
  FROM template
  WHERE merchant_id IS NULL
  GROUP BY reseller_id
), traffic AS (          -- leads pushed with this merchant's identity
  SELECT reseller_id, merchant_id, COUNT(*) AS leads
  FROM lead_call_tracker
  WHERE created_at >= now() - (:'days')::int * interval '1 day'
    AND merchant_id IS NOT NULL
  GROUP BY 1, 2
)
SELECT m.reseller_id,
       m.merchant_id,
       m.name                                   AS merchant_name,
       m.created_at::date                       AS onboarded,
       (now()::date - m.created_at::date)       AS days_since_onboard,
       COALESCE(tr.leads, 0)                    AS leads_last_period,
       COALESCE(sn.shared_templates, '— none: reseller has no shared templates either!')
                                                AS falling_back_on
FROM merchants m
LEFT JOIN own          o  ON o.reseller_id  = m.reseller_id
                         AND o.merchant_id  = m.merchant_id
LEFT JOIN shared_names sn ON sn.reseller_id = m.reseller_id
LEFT JOIN traffic      tr ON tr.reseller_id = m.reseller_id
                         AND tr.merchant_id = m.merchant_id
WHERE m.is_active
  AND o.merchant_id IS NULL
  -- AND m.reseller_id = :'reseller'
ORDER BY m.created_at DESC;
