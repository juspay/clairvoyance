#!/usr/bin/env bash
# Read-only report runner for fleet_census.sql / orphan_merchants.sql.
#
# Connection comes from the standard libpq env vars — export them yourself,
# never commit them:
#   export PGHOST=127.0.0.1 PGPORT=5433 PGDATABASE=clairvoyance_db \
#          PGUSER=clairvoyance_rw PGPASSWORD='...'
# (with prod behind k8s: kubectl port-forward svc/<pg-svc> 5433:5432 first)
#
# Usage:
#   DAYS=30 ./scripts/db_readonly_report.sh fleet_census.sql
#   DAYS=30 ./scripts/db_readonly_report.sh orphan_merchants.sql
#
# Both SQL files force the session READ ONLY as their first statement, so
# even with rw credentials no write can occur.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
FILE="${1:-fleet_census.sql}"
DAYS="${DAYS:-30}"

[[ -f "$HERE/$FILE" ]] || { echo "no such report: $FILE" >&2; exit 1; }
: "${PGHOST:?export PGHOST (see header)}"

echo "── running $FILE against $PGDATABASE@$PGHOST:${PGPORT:-5432} as $PGUSER (READ ONLY, days=$DAYS)" >&2
exec psql -X -v ON_ERROR_STOP=1 -v days="$DAYS" ${PSQL_ARGS:-} -f "$HERE/$FILE"
