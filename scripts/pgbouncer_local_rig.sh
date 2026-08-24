#!/usr/bin/env bash
# Local Postgres 14 + PgBouncer (transaction mode) rig for PgBouncer-compat tests.
#
# Mirrors the production plan (P0.1): PgBouncer in pool_mode=transaction in
# front of Postgres, with max_prepared_statements=0 so the rig exposes the
# raw prepared-statement incompatibility rather than papering over it with
# PgBouncer >=1.21 statement tracking. Cloud SQL Managed Connection Pooling
# behaves the same way unless max_prepared_statements is raised, so this is
# the strictest environment the app must survive.
#
# Usage:
#   scripts/pgbouncer_local_rig.sh start | stop | status
#
# Then run the integration tests against it:
#   PGBOUNCER_RIG=1 uv run pytest tests/database/test_pgbouncer_compat.py -v
#
# Requirements: postgresql@14 and pgbouncer on PATH (brew install postgresql@14 pgbouncer).
# Override RIG_DIR / PG_PORT / BOUNCER_PORT via env if the defaults clash.

set -euo pipefail

RIG_DIR="${RIG_DIR:-${TMPDIR:-/tmp}/clairvoyance-pgbouncer-rig}"
PG_PORT="${PG_PORT:-55432}"
BOUNCER_PORT="${BOUNCER_PORT:-56432}"
PG_USER="clairvoyance"
PG_PASSWORD="clairpass"
PG_DB="clairdb"

PG_BIN="${PG_BIN:-$(dirname "$(command -v pg_ctl)")}"
PGDATA="$RIG_DIR/pgdata"
SOCKET_DIR="$RIG_DIR/sockets"
BOUNCER_INI="$RIG_DIR/pgbouncer.ini"
BOUNCER_LOG="$RIG_DIR/pgbouncer.log"
BOUNCER_PID="$RIG_DIR/pgbouncer.pid"

start() {
    command -v pgbouncer >/dev/null || { echo "pgbouncer not on PATH (brew install pgbouncer)"; exit 1; }

    # 1.21 is the floor: below it cancel-request peering and
    # max_prepared_statements don't exist, so an old bouncer would pass this
    # suite while behaving differently from production (Ubuntu 22.04 still
    # ships 1.16). Fail loudly rather than test the wrong thing.
    have_version=$(pgbouncer --version | head -1 | awk '{print $2}')
    if [ "$(printf '%s\n1.21\n' "$have_version" | sort -V | head -1)" != "1.21" ]; then
        echo "pgbouncer $have_version is too old — need >= 1.21 (>= 1.25.2 recommended)"
        exit 1
    fi
    [ -x "$PG_BIN/initdb" ] || { echo "postgres binaries not found (brew install postgresql@14)"; exit 1; }

    mkdir -p "$RIG_DIR" "$SOCKET_DIR"

    if [ ! -d "$PGDATA" ]; then
        echo "$PG_PASSWORD" > "$RIG_DIR/pwfile"
        "$PG_BIN/initdb" -D "$PGDATA" -U "$PG_USER" --pwfile="$RIG_DIR/pwfile" \
            --auth=scram-sha-256 -E UTF8 >/dev/null
        rm "$RIG_DIR/pwfile"
    fi

    "$PG_BIN/pg_ctl" -D "$PGDATA" -l "$RIG_DIR/postgres.log" \
        -o "-p $PG_PORT -k $SOCKET_DIR -c listen_addresses=127.0.0.1" start >/dev/null

    PGPASSWORD="$PG_PASSWORD" "$PG_BIN/psql" -h 127.0.0.1 -p "$PG_PORT" -U "$PG_USER" -d postgres \
        -tc "SELECT 1 FROM pg_database WHERE datname='$PG_DB'" | grep -q 1 ||
        PGPASSWORD="$PG_PASSWORD" "$PG_BIN/createdb" -h 127.0.0.1 -p "$PG_PORT" -U "$PG_USER" "$PG_DB"
    PGPASSWORD="$PG_PASSWORD" "$PG_BIN/psql" -h 127.0.0.1 -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB" \
        -c "CREATE EXTENSION IF NOT EXISTS pg_trgm" >/dev/null

    cat > "$RIG_DIR/userlist.txt" <<EOF
"$PG_USER" "$PG_PASSWORD"
EOF

    cat > "$BOUNCER_INI" <<EOF
[databases]
$PG_DB = host=127.0.0.1 port=$PG_PORT dbname=$PG_DB

[pgbouncer]
listen_addr = 127.0.0.1
listen_port = $BOUNCER_PORT
unix_socket_dir = $SOCKET_DIR
auth_type = scram-sha-256
auth_file = $RIG_DIR/userlist.txt
pool_mode = transaction
; Strict mode: no server-side tracking of named prepared statements.
; asyncpg must therefore not create them (statement_cache_size=0).
max_prepared_statements = 0
default_pool_size = 2
max_client_conn = 200
logfile = $BOUNCER_LOG
pidfile = $BOUNCER_PID
EOF

    pgbouncer -d "$BOUNCER_INI" >/dev/null 2>&1
    sleep 1
    status
}

stop() {
    [ -f "$BOUNCER_PID" ] && kill "$(cat "$BOUNCER_PID")" 2>/dev/null || true
    [ -d "$PGDATA" ] && "$PG_BIN/pg_ctl" -D "$PGDATA" stop -m fast >/dev/null 2>&1 || true
    echo "rig stopped ($RIG_DIR retained; delete it for a clean slate)"
}

status() {
    echo "rig dir:   $RIG_DIR"
    if "$PG_BIN/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1; then
        echo "postgres:  running on 127.0.0.1:$PG_PORT (db=$PG_DB user=$PG_USER)"
    else
        echo "postgres:  NOT running"
    fi
    if [ -f "$BOUNCER_PID" ] && kill -0 "$(cat "$BOUNCER_PID")" 2>/dev/null; then
        echo "pgbouncer: running on 127.0.0.1:$BOUNCER_PORT (transaction mode, max_prepared_statements=0)"
    else
        echo "pgbouncer: NOT running"
    fi
}

case "${1:-}" in
    start) start ;;
    stop) stop ;;
    status) status ;;
    *) echo "usage: $0 start|stop|status"; exit 1 ;;
esac
