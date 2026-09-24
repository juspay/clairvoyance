-- ═══════════════════════════════════════════════════════════════════════
-- FLEET CENSUS (read-only) — shared-template landscape across ALL resellers
--
-- Sizes the "remove shared templates, one template per merchant" backfill:
--   1. every reseller + merchant/template counts
--   2. shared-template inventory (whatever names exist per reseller —
--      order-confirmation, fire-abandonment, anything)
--   3. coverage matrix: per shared template, who has their own copy vs
--      who is silently falling back
--   4. traffic reality over the last :days days (which scope actually
--      served leads, and whether pushers send merchant_id at all)
--   5. attachments hanging off shared rows (numbers / widget / configs)
--   6. orphan-merchant preview (full report: orphan_merchants.sql)
--
-- Run:  DAYS=30 ./scripts/db_readonly_report.sh fleet_census.sql
-- Every statement is a SELECT; the session is forced READ ONLY below.
-- ═══════════════════════════════════════════════════════════════════════
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
\pset pager off
\timing off

\echo ''
\echo '══ 0. Connection context ═════════════════════════════════════════'
SELECT current_database() AS db, current_user AS "user", now() AS at;

\echo ''
\echo '══ 1. Resellers overview ═════════════════════════════════════════'
SELECT r.reseller_id,
       COUNT(DISTINCT m.merchant_id) FILTER (WHERE m.is_active)     AS active_merchants,
       COUNT(DISTINCT m.merchant_id)                                AS total_merchants,
       COUNT(DISTINCT t.id) FILTER (WHERE t.merchant_id IS NULL)            AS shared_templates,
       COUNT(DISTINCT t.id) FILTER (WHERE t.merchant_id IS NOT NULL)        AS merchant_templates
FROM (SELECT reseller_id FROM template
      UNION SELECT reseller_id FROM merchants) r
LEFT JOIN merchants m ON m.reseller_id = r.reseller_id
LEFT JOIN template  t ON t.reseller_id = r.reseller_id
GROUP BY r.reseller_id
ORDER BY active_merchants DESC NULLS LAST, r.reseller_id;

\echo ''
\echo '══ 2. Shared-template inventory (merchant_id IS NULL rows) ═══════'
SELECT reseller_id,
       name,
       id,
       is_active,
       supported_channels,
       (outbound_number_id IS NOT NULL) AS has_number,
       updated_at::date                 AS last_updated
FROM template
WHERE merchant_id IS NULL
ORDER BY reseller_id, name;

\echo ''
\echo '══ 3. Coverage matrix: own copy vs falling back, per shared name ═'
WITH shared AS (
  SELECT reseller_id, name FROM template WHERE merchant_id IS NULL
), mm AS (
  SELECT reseller_id, merchant_id AS merchant_identifier FROM merchants WHERE is_active
)
SELECT s.reseller_id,
       s.name                                                            AS shared_template,
       (SELECT COUNT(*) FROM mm WHERE mm.reseller_id = s.reseller_id)    AS active_merchants,
       COUNT(DISTINCT t.merchant_id)                                     AS merchants_with_own_copy,
       (SELECT COUNT(*) FROM mm WHERE mm.reseller_id = s.reseller_id)
         - COUNT(DISTINCT t.merchant_id)                                 AS falling_back
FROM shared s
LEFT JOIN template t
       ON t.reseller_id = s.reseller_id
      AND t.name        = s.name
      AND t.merchant_id IS NOT NULL
GROUP BY s.reseller_id, s.name
ORDER BY falling_back DESC, s.reseller_id, s.name;

\echo ''
\echo '══ 4a. Traffic reality, last :days days, by serving scope ════════'
\echo '    lead_has_merchant_id=false ⇒ the pusher omits merchant identity'
\echo '    (those leads can NEVER pick up a merchant copy — upstream fix needed)'
SELECT l.reseller_id,
       CASE WHEN t.id IS NULL          THEN 'no-template-row (stale id/legacy)'
            WHEN t.merchant_id IS NULL THEN 'SHARED template'
            ELSE                            'merchant-specific' END       AS serving_scope,
       (l.merchant_id IS NOT NULL)                                       AS lead_has_merchant_id,
       COUNT(*)                                                          AS leads
FROM lead_call_tracker l
LEFT JOIN template t ON t.id = l.template_id
WHERE l.created_at >= now() - (:'days')::int * interval '1 day'
GROUP BY 1, 2, 3
ORDER BY l.reseller_id, leads DESC;

\echo ''
\echo '══ 4b. Busiest SHARED templates, last :days days ═════════════════'
SELECT l.reseller_id,
       t.name,
       COUNT(*)                       AS leads,
       COUNT(DISTINCT l.merchant_id)  AS distinct_merchants_served
FROM lead_call_tracker l
JOIN template t ON t.id = l.template_id AND t.merchant_id IS NULL
WHERE l.created_at >= now() - (:'days')::int * interval '1 day'
GROUP BY 1, 2
ORDER BY leads DESC
LIMIT 20;

\echo ''
\echo '══ 5a. Shared templates holding a phone number (inbound risk) ════'
SELECT reseller_id, name, id, outbound_number_id
FROM template
WHERE merchant_id IS NULL AND outbound_number_id IS NOT NULL
ORDER BY reseller_id, name;

\echo ''
\echo '══ 5b. Widget configs pointing at SHARED template rows ═══════════'
SELECT w.reseller_id, w.merchant_id, t.name AS shared_template, t.id AS template_id
FROM widget_config w
JOIN template t ON t.id = w.template_id
WHERE t.merchant_id IS NULL
ORDER BY w.reseller_id, w.merchant_id;

\echo ''
\echo '══ 5c. Calling configs: shared (merchant_id IS NULL) rows ═════════'
SELECT reseller_id, template AS template_name, COUNT(*) AS shared_config_rows
FROM call_execution_config
WHERE merchant_id IS NULL
GROUP BY 1, 2
ORDER BY 1, 2;

\echo ''
\echo '══ 5d. Calling configs: merchant-specific counts per reseller ════'
SELECT reseller_id, COUNT(*) AS merchant_config_rows
FROM call_execution_config
WHERE merchant_id IS NOT NULL
GROUP BY 1
ORDER BY 2 DESC;

\echo ''
\echo '══ 6. Orphan-merchant preview (no template of their own at all) ══'
\echo '    Full per-merchant report: scripts/orphan_merchants.sql'
SELECT m.reseller_id,
       COUNT(*)                                              AS orphan_merchants,
       MIN(m.created_at)::date                               AS oldest_onboard,
       MAX(m.created_at)::date                               AS newest_onboard
FROM merchants m
LEFT JOIN (SELECT DISTINCT reseller_id, merchant_id
           FROM template WHERE merchant_id IS NOT NULL) o
       ON o.reseller_id = m.reseller_id
      AND o.merchant_id = m.merchant_id
WHERE m.is_active AND o.merchant_id IS NULL
GROUP BY m.reseller_id
ORDER BY orphan_merchants DESC;
