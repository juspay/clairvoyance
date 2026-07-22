"""SQLite metadata store (stdlib sqlite3, wrapped with asyncio.to_thread).

Each worker thread owns its own connection (thread-local), fetched INSIDE the
threaded call via :meth:`_run`. This is what makes the store safe and parallel
under concurrency: WAL then allows many concurrent readers across the
per-thread connections and a single serialized writer, and writes can't corrupt
the way they would if one event-loop-thread connection were shared across
workers (``check_same_thread=False`` + sqlite3 threadsafety=1 is unsafe for
concurrent use of a single connection).
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypeVar

from app.core.logging import logger
from app.storage.base import CacheRecord, escape_like

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache_entries (
    key              TEXT PRIMARY KEY,
    provider         TEXT NOT NULL,
    voice_id         TEXT NOT NULL,
    model            TEXT NOT NULL,
    language         TEXT NOT NULL,
    params           TEXT NOT NULL DEFAULT '',
    text             TEXT,
    container        TEXT NOT NULL,
    encoding         TEXT NOT NULL,
    sample_rate      INTEGER NOT NULL,
    size_bytes       INTEGER NOT NULL,
    storage_path     TEXT NOT NULL,
    hit_count        INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    last_accessed_at TEXT NOT NULL,
    ttl_expires_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_provider_voice ON cache_entries(provider, voice_id);

-- Request metrics rolled up per UTC day (date-filterable via SUM, never a scan
-- of cache_entries).
CREATE TABLE IF NOT EXISTS metrics_daily (
    date            TEXT PRIMARY KEY,
    requests        INTEGER NOT NULL DEFAULT 0,
    hits            INTEGER NOT NULL DEFAULT 0,
    misses          INTEGER NOT NULL DEFAULT 0,
    bytes_served    INTEGER NOT NULL DEFAULT 0,
    synth_calls     INTEGER NOT NULL DEFAULT 0,
    base64_uploads  INTEGER NOT NULL DEFAULT 0,
    creates         INTEGER NOT NULL DEFAULT 0,
    deletes         INTEGER NOT NULL DEFAULT 0,
    words_served             INTEGER NOT NULL DEFAULT 0,
    words_synthesized        INTEGER NOT NULL DEFAULT 0,
    stitch_calls             INTEGER NOT NULL DEFAULT 0,
    stitch_words_assembled   INTEGER NOT NULL DEFAULT 0,
    stitch_words_synthesized INTEGER NOT NULL DEFAULT 0
);

-- Per-provider daily rollup (hit rate / synth calls per provider).
CREATE TABLE IF NOT EXISTS metrics_daily_provider (
    date         TEXT NOT NULL,
    provider     TEXT NOT NULL,
    requests     INTEGER NOT NULL DEFAULT 0,
    hits         INTEGER NOT NULL DEFAULT 0,
    misses       INTEGER NOT NULL DEFAULT 0,
    synth_calls  INTEGER NOT NULL DEFAULT 0,
    bytes_served INTEGER NOT NULL DEFAULT 0,
    words_served INTEGER NOT NULL DEFAULT 0,
    words_synthesized INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (date, provider)
);

-- Latency samples for the avg/p95 rollup (day-filtered). Bounded by sample rate
-- + retention prune; auto-rowid, indexed (kind, date) for count + sorted p95.
CREATE TABLE IF NOT EXISTS latency_samples (
    date        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    latency_us  INTEGER NOT NULL,
    provider    TEXT
);
CREATE INDEX IF NOT EXISTS idx_latency_kind_date ON latency_samples(kind, date);

-- Once-daily Slack summary coordination across uvicorn workers (DragonTTS has no
-- Redis, so this table is the shared claim). key='summary_last_post', value =
-- today's UTC date once a worker has posted. See claim_slack_summary/release.
CREATE TABLE IF NOT EXISTS slack_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

-- Cache snapshot maintained incrementally so /stats is O(providers), not a
-- full-table GROUP BY.
CREATE TABLE IF NOT EXISTS provider_totals (
    provider    TEXT PRIMARY KEY,
    entries     INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    total_words INTEGER NOT NULL DEFAULT 0
);
"""

_COLUMNS = (
    "key",
    "provider",
    "voice_id",
    "model",
    "language",
    "params",
    "text",
    "container",
    "encoding",
    "sample_rate",
    "size_bytes",
    "storage_path",
    "hit_count",
    "created_at",
    "last_accessed_at",
    "ttl_expires_at",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_record(row: sqlite3.Row) -> CacheRecord:
    d = dict(row)
    return CacheRecord(**{c: d[c] for c in _COLUMNS})


def _wc(text: str | None) -> int:
    """Word count of a stored transcript (whitespace tokens, punctuation kept)."""
    return len((text or "").split())


def _word_count_sql(column: str = "text") -> str:
    """SQL word count of a TEXT column (whitespace tokens); NULL/empty -> 0.
    Accurate for normalize_text output (whitespace already collapsed)."""
    return (
        f"CASE WHEN {column} IS NULL OR {column} = '' THEN 0 "
        f"ELSE LENGTH({column}) - LENGTH(REPLACE({column}, ' ', '')) + 1 END"
    )


# Columns mirrored into metrics_daily_provider when record_metrics/touch_and_record
# carry a provider (the per-provider-relevant subset of the daily counters).
_PROVIDER_COLS = ("requests", "hits", "misses", "synth_calls", "bytes_served", "words_served", "words_synthesized")

# Idempotent column additions for existing DBs (ALTER ... ADD COLUMN ... DEFAULT 0).
_METRICS_MIGRATIONS = [
    ("metrics_daily", "words_served INTEGER NOT NULL DEFAULT 0"),
    ("metrics_daily", "words_synthesized INTEGER NOT NULL DEFAULT 0"),
    ("metrics_daily", "stitch_calls INTEGER NOT NULL DEFAULT 0"),
    ("metrics_daily", "stitch_words_assembled INTEGER NOT NULL DEFAULT 0"),
    ("metrics_daily", "stitch_words_synthesized INTEGER NOT NULL DEFAULT 0"),
    ("provider_totals", "total_words INTEGER NOT NULL DEFAULT 0"),
    # Per-provider words_synthesized (enables per-provider words-from-cache % +
    # per-provider cost-saved in the Slack summary). The data was already routed
    # here (every synth-path record_metrics passes provider=), it just wasn't
    # persisted before this column existed.
    ("metrics_daily_provider", "words_synthesized INTEGER NOT NULL DEFAULT 0"),
    # Per-provider latency. Nullable so pre-existing rows stay NULL (the
    # by-provider query filters provider IS NOT NULL). Idempotent ADD COLUMN.
    ("latency_samples", "provider TEXT"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    """Add new columns to existing tables (no-op on a fresh DB, which already has
    them via _SCHEMA). SQLite ADD COLUMN ... DEFAULT 0 is online/non-locking.
    Each check+ALTER is wrapped in BEGIN IMMEDIATE so the table_info guard and
    the ADD COLUMN are atomic across worker processes (see comment below)."""
    for table, ddl in _METRICS_MIGRATIONS:
        col = ddl.split()[0]
        # BEGIN IMMEDIATE makes the table_info check + ADD COLUMN atomic ACROSS
        # worker processes: without it, two uvicorn workers starting against the
        # same PVC DB can both read "column absent" then both ALTER, the second
        # failing with 'duplicate column name' and crash-looping that worker.
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    # Indexes that depend on a migrated column are created here (after the ALTER),
    # NOT in _SCHEMA — _SCHEMA runs before _migrate and would reference a
    # not-yet-existing column on an old DB (the provider index failed that way).
    # Idempotent via IF NOT EXISTS; a no-op once it exists.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_latency_provider_kind_date "
        "ON latency_samples(provider, kind, date)"
    )


T = TypeVar("T")


class SQLiteMetadataStore:
    """Async-friendly metadata store backed by a single SQLite file (WAL mode)."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()

    def _new_connection(self) -> sqlite3.Connection:
        # One connection per OS thread. WAL permits many concurrent readers
        # across connections and a single serialized writer; busy_timeout makes
        # contending writers wait instead of raising "database is locked".
        # isolation_level=None => autocommit. Our ops are single-statement
        # (point lookups / single inserts); autocommit avoids implicit nested
        # transactions ("cannot start a transaction within a transaction") on
        # long-lived, reused thread connections.
        conn = sqlite3.connect(
            self.db_path, timeout=30, isolation_level=None, check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        # Match connect()'s timeout (30s): contending writers retry up to 30s
        # before raising SQLITE_BUSY.
        conn.execute("PRAGMA busy_timeout=30000")
        # Per-connection page cache. With one connection per worker thread,
        # keep this modest (8MB) so N connections stay memory-bounded; the
        # 256MB mmap below carries the real read working set (OS page cache).
        conn.execute("PRAGMA cache_size = -2048")  # ~2MB page cache (was 8MB) -- the mmap + OS page cache carry the real read working set, so keep this small; bounds per-connection RSS x (THREAD_POOL_WORKERS conns) x (--workers processes) under the pod's memory limit
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA mmap_size = 67108864")  # 64MB mmap for reads (was 256MB) -- bounds resident mmap pages under the cgroup limit; hot reads still come from the OS page cache
        return conn

    async def init(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        def _setup() -> None:
            conn = self._new_connection()
            try:
                conn.executescript(_SCHEMA)
                _migrate(conn)  # add new columns to an existing DB (no-op if fresh)
                # Backfill snapshot totals if empty but entries exist (handles a
                # migration from before provider_totals existed).
                has_totals = conn.execute("SELECT COUNT(*) FROM provider_totals").fetchone()[0]
                if has_totals == 0:
                    conn.execute(
                        "INSERT INTO provider_totals (provider, entries, total_bytes, total_words) "
                        "SELECT provider, COUNT(*), COALESCE(SUM(size_bytes), 0), "
                        f"COALESCE(SUM({_word_count_sql('text')}), 0) "
                        "FROM cache_entries GROUP BY provider "
                        "ON CONFLICT(provider) DO UPDATE SET "
                        "entries = excluded.entries, total_bytes = excluded.total_bytes, "
                        "total_words = excluded.total_words"
                    )
                # Backfill total_words from cache_entries ONLY on the first start
                # after the migration (entries exist but total_words is still the 0
                # default) — avoids a per-restart scan of a growing live cache.
                has_entries = conn.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0] > 0
                words_zero = (
                    conn.execute("SELECT COALESCE(SUM(total_words), 0) FROM provider_totals").fetchone()[0] == 0
                )
                if has_entries and words_zero:
                    conn.execute(
                        "UPDATE provider_totals SET total_words = COALESCE("
                        f"(SELECT SUM({_word_count_sql('cache_entries.text')}) "
                        "FROM cache_entries WHERE cache_entries.provider = provider_totals.provider), 0)"
                    )
                # Compact any leftover -wal from a prior run now that the schema
                # is open (runtime growth is bounded by the periodic checkpoint).
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception:
                    pass
            finally:
                conn.close()

        await asyncio.to_thread(_setup)
        logger.info(f"SQLite metadata store ready at {self.db_path}")

    async def checkpoint(self) -> None:
        """Run a TRUNCATE WAL checkpoint so the ``-wal`` file is compacted back
        into the main db. Passive auto-checkpoint (1000 frames) moves frames but
        doesn't shrink the ``-wal`` while worker connections stay open, so this
        is run on startup, periodically, and on shutdown to keep it bounded on
        the PVC. Best-effort: never fails a request over a checkpoint error."""

        def _cp(conn: sqlite3.Connection) -> None:
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass

        await self._run(_cp)

    def _conn(self) -> sqlite3.Connection:
        """Lazily-created, thread-local connection (cached for reuse). MUST be
        called from the worker thread that will use it — see :meth:`_run`."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
        return conn

    async def _run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run ``fn(connection)`` on a worker thread using THAT thread's own
        connection. This is the concurrency-safe path: every worker thread gets
        its own SQLite connection, so reads parallelize under WAL and concurrent
        writes don't corrupt (no shared event-loop-thread connection)."""
        return await asyncio.to_thread(lambda: fn(self._conn()))

    async def get(self, key: str) -> CacheRecord | None:
        def _q(conn: sqlite3.Connection) -> sqlite3.Row | None:
            return conn.execute(
                "SELECT * FROM cache_entries WHERE key = ?", (key,)
            ).fetchone()

        row = await self._run(_q)
        return _row_to_record(row) if row else None

    async def get_many(self, keys) -> dict[str, CacheRecord]:
        """Return stored records for the given keys (only those present).

        Batched point lookup for stitch's candidate-sub-span probe: one
        ``key IN (...)`` query instead of N individual ``get``s. Chunked to
        stay under SQLite's host-parameter limit.
        """
        keys = [k for k in dict.fromkeys(keys) if k]  # dedupe, drop empties
        if not keys:
            return {}
        out: dict[str, CacheRecord] = {}
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]

            def _q(conn: sqlite3.Connection, chunk=chunk) -> list:
                placeholders = ",".join("?" for _ in chunk)
                return conn.execute(
                    f"SELECT * FROM cache_entries WHERE key IN ({placeholders})", chunk
                ).fetchall()

            for row in await self._run(_q):
                rec = _row_to_record(row)
                if rec:
                    out[rec.key] = rec
        return out

    async def put(self, record: CacheRecord) -> None:
        values = tuple(getattr(record, c) for c in _COLUMNS)
        placeholders = ",".join("?" for _ in _COLUMNS)
        sql = (
            f"INSERT OR REPLACE INTO cache_entries ({','.join(_COLUMNS)}) "
            f"VALUES ({placeholders})"
        )

        def _w(conn: sqlite3.Connection) -> None:
            conn.execute(sql, values)
            conn.commit()

        await self._run(_w)

    async def put_with_totals(self, record: CacheRecord) -> None:
        """Insert a NEW cache row; bump provider_totals only if this call
        actually inserted it.

        ``INSERT OR IGNORE`` + rowcount is race-free under WAL: writers
        serialize, so concurrent stores of the SAME key produce exactly one
        insert and one totals bump — ``provider_totals`` can't drift. Each
        statement autocommits (no explicit BEGIN, which conflicts with
        isolation_level=None on reused thread-local connections). For overrides
        and expired-refresh use ``put`` (REPLACE) + ``adjust_totals``.
        """
        values = tuple(getattr(record, c) for c in _COLUMNS)
        placeholders = ",".join("?" for _ in _COLUMNS)
        insert_sql = (
            f"INSERT OR IGNORE INTO cache_entries ({','.join(_COLUMNS)}) "
            f"VALUES ({placeholders})"
        )
        totals_sql = (
            "INSERT INTO provider_totals (provider, entries, total_bytes, total_words) "
            "VALUES (?, 1, ?, ?) "
            "ON CONFLICT(provider) DO UPDATE SET "
            "entries = entries + 1, total_bytes = total_bytes + excluded.total_bytes, "
            "total_words = total_words + excluded.total_words"
        )

        def _w(conn: sqlite3.Connection) -> None:
            # Atomic: the row insert and the provider_totals bump must both land
            # or neither. Under isolation_level=None each execute autocommits
            # independently, so wrap the pair in an explicit transaction.
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(insert_sql, values)
                if cur.rowcount == 1:  # newly inserted; a concurrent dup was ignored
                    conn.execute(totals_sql, (record.provider, record.size_bytes, _wc(record.text)))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

        await self._run(_w)

    async def replace_with_totals(self, record: CacheRecord) -> None:
        """REPLACE an existing row and adjust ``provider_totals`` atomically.

        Used by the refresh/override store path. ``put`` + ``adjust_totals``
        separately would not be atomic: a concurrent ``delete`` of the same key
        landing between them would drift ``provider_totals`` (delete decrements,
        then the stale adjust re-applies a delta against a row that's gone). The
        current row's size is re-read INSIDE the ``BEGIN IMMEDIATE`` transaction
        so the delta reflects the row's actual state under the write lock.
        ``provider`` can't change (it's part of the key), so only the size delta
        matters for an existing row; a missing prior row is a fresh insert (+1).
        """
        values = tuple(getattr(record, c) for c in _COLUMNS)
        placeholders = ",".join("?" for _ in _COLUMNS)
        replace_sql = (
            f"INSERT OR REPLACE INTO cache_entries ({','.join(_COLUMNS)}) "
            f"VALUES ({placeholders})"
        )
        totals_sql = (
            "INSERT INTO provider_totals (provider, entries, total_bytes, total_words) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(provider) DO UPDATE SET "
            "entries = entries + excluded.entries, total_bytes = total_bytes + excluded.total_bytes, "
            "total_words = total_words + excluded.total_words"
        )

        def _w(conn: sqlite3.Connection) -> None:
            conn.execute("BEGIN IMMEDIATE")
            try:
                prev = conn.execute(
                    "SELECT size_bytes, text FROM cache_entries WHERE key = ?", (record.key,)
                ).fetchone()
                conn.execute(replace_sql, values)
                new_wc = _wc(record.text)
                if prev is None:
                    conn.execute(totals_sql, (record.provider, 1, record.size_bytes, new_wc))
                else:
                    conn.execute(
                        totals_sql,
                        (record.provider, 0, record.size_bytes - prev[0], new_wc - _wc(prev[1])),
                    )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

        await self._run(_w)

    async def touch(self, key: str) -> None:
        def _t(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE cache_entries SET hit_count = hit_count + 1, "
                "last_accessed_at = ? WHERE key = ?",
                (_now(), key),
            )
            conn.commit()

        await self._run(_t)

    async def delete(self, key: str) -> bool:
        """Delete one row AND adjust provider_totals atomically.

        SELECT + DELETE + totals-adjust run in one ``BEGIN IMMEDIATE``
        transaction, so the captured size is the row's actual size at delete
        time (a concurrent override of the same key can't make totals drift)
        and a concurrent insert can't escape. Returns True iff a row was deleted.
        """
        totals_sql = (
            "INSERT INTO provider_totals (provider, entries, total_bytes, total_words) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(provider) DO UPDATE SET "
            "entries = entries + excluded.entries, total_bytes = total_bytes + excluded.total_bytes, "
            "total_words = total_words + excluded.total_words"
        )

        def _d(conn: sqlite3.Connection) -> bool:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT provider, size_bytes, text FROM cache_entries WHERE key = ?", (key,)
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return False
                conn.execute("DELETE FROM cache_entries WHERE key = ?", (key,))
                conn.execute(totals_sql, (row[0], -1, -row[1], -_wc(row[2])))
                conn.execute("COMMIT")
                return True
            except BaseException:
                conn.execute("ROLLBACK")
                raise

        return await self._run(_d)

    async def list(
        self,
        provider: str | None = None,
        voice_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        q: str | None = None,
        match: str = "exact",
        created_before: str | None = None,
        created_after: str | None = None,
    ) -> list[CacheRecord]:
        clauses: list[str] = []
        args: list = []
        if provider:
            clauses.append("provider = ?")
            args.append(provider)
        if voice_id:
            clauses.append("voice_id = ?")
            args.append(voice_id)
        if q:
            if match.lower() == "substring":
                # ESCAPE '\\' so a user '%'/_'_' in q matches literally, not as a
                # wildcard (else q='_' would match every single-char text row).
                clauses.append("text LIKE ? ESCAPE '\\'")
                args.append(f"%{escape_like(q)}%")
            else:
                clauses.append("text = ?")
                args.append(q)
        # created_at is a full ISO timestamp; bare-date args compare sensibly:
        # created_after=YYYY-MM-DD is inclusive of that whole day (timestamp sorts
        # after the bare date); created_before=YYYY-MM-DD is exclusive (strictly
        # before that day's start).
        if created_after:
            clauses.append("created_at >= ?")
            args.append(created_after)
        if created_before:
            clauses.append("created_at < ?")
            args.append(created_before)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        args.append(limit)
        args.append(offset)

        def _q(conn: sqlite3.Connection) -> list[sqlite3.Row]:
            return conn.execute(
                f"SELECT * FROM cache_entries{where} ORDER BY created_at DESC "
                f"LIMIT ? OFFSET ?",
                args,
            ).fetchall()

        rows = await self._run(_q)
        return [_row_to_record(r) for r in rows]

    async def all_keys(self) -> set[str]:
        """Every cache key — used to reap orphaned blob files."""

        def _q(conn: sqlite3.Connection) -> set[str]:
            return {row[0] for row in conn.execute("SELECT key FROM cache_entries")}

        return await self._run(_q)

    async def delete_filtered(
        self, provider: str | None = None, voice_id: str | None = None
    ) -> list[tuple]:
        """Bulk-delete matching rows AND adjust provider_totals atomically;
        return (provider, size_bytes, storage_path) per row for blob cleanup.

        SELECT + DELETE + per-provider totals-adjust run in one ``BEGIN
        IMMEDIATE`` transaction, so a concurrent insert can't escape the clear
        and totals can't drift.
        """
        clauses: list[str] = []
        args: list = []
        if provider:
            clauses.append("provider = ?")
            args.append(provider)
        if voice_id:
            clauses.append("voice_id = ?")
            args.append(voice_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        totals_sql = (
            "INSERT INTO provider_totals (provider, entries, total_bytes, total_words) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(provider) DO UPDATE SET "
            "entries = entries + excluded.entries, total_bytes = total_bytes + excluded.total_bytes, "
            "total_words = total_words + excluded.total_words"
        )

        def _d(conn: sqlite3.Connection) -> list[tuple]:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    f"SELECT provider, size_bytes, storage_path, text FROM cache_entries{where}", args
                ).fetchall()
                if rows:
                    conn.execute(f"DELETE FROM cache_entries{where}", args)
                    # Sum (entries, bytes, words) deltas per provider, apply once each.
                    deltas: dict[str, list[int]] = {}
                    for r in rows:
                        d = deltas.setdefault(r[0], [0, 0, 0])
                        d[0] -= 1
                        d[1] -= r[1]
                        d[2] -= _wc(r[3])
                    for prov, (de, db, dw) in deltas.items():
                        conn.execute(totals_sql, (prov, de, db, dw))
                conn.execute("COMMIT")
                return [(r[0], r[1], r[2]) for r in rows]
            except BaseException:
                conn.execute("ROLLBACK")
                raise

        return await self._run(_d)

    async def purge_expired(self, now_iso: str) -> list[tuple]:
        """Delete rows whose ``ttl_expires_at`` is set and < ``now_iso``, adjusting
        ``provider_totals`` atomically; return ``(provider, size_bytes,
        storage_path)`` per row so the caller can unlink the blobs. Mirrors
        :meth:`delete_filtered`'s transaction so a concurrent insert can't escape
        the purge or drift totals. Rows with NULL ``ttl_expires_at`` (never
        expiring) are left alone."""
        where = " WHERE ttl_expires_at IS NOT NULL AND ttl_expires_at < ?"
        totals_sql = (
            "INSERT INTO provider_totals (provider, entries, total_bytes, total_words) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(provider) DO UPDATE SET "
            "entries = entries + excluded.entries, total_bytes = total_bytes + excluded.total_bytes, "
            "total_words = total_words + excluded.total_words"
        )

        def _d(conn: sqlite3.Connection) -> list[tuple]:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    f"SELECT provider, size_bytes, storage_path, text FROM cache_entries{where}",
                    (now_iso,),
                ).fetchall()
                if rows:
                    conn.execute(f"DELETE FROM cache_entries{where}", (now_iso,))
                    deltas: dict[str, list[int]] = {}
                    for r in rows:
                        d = deltas.setdefault(r[0], [0, 0, 0])
                        d[0] -= 1
                        d[1] -= r[1]
                        d[2] -= _wc(r[3])
                    for prov, (de, db, dw) in deltas.items():
                        conn.execute(totals_sql, (prov, de, db, dw))
                conn.execute("COMMIT")
                return [(r[0], r[1], r[2]) for r in rows]
            except BaseException:
                conn.execute("ROLLBACK")
                raise

        return await self._run(_d)

    async def backfill_missing_ttl(self, min_hours: int, max_hours: int) -> int:
        """Set a RANDOM expiry in ``[min_hours, max_hours]`` from now on every row
        whose ``ttl_expires_at`` is NULL (pre-existing entries created before
        length-scaled TTL, e.g. under ``ttl_seconds=0``). Idempotent — only
        touches NULL rows, so re-running on restart is a no-op. Returns the
        number of rows backfilled.

        ``abs(random()) % span`` is evaluated per row, so expiries spread across
        the window instead of expiring en masse (which would spike re-synths).
        """
        if max_hours < min_hours:
            max_hours = min_hours
        span = max(1, max_hours - min_hours + 1)  # inclusive [min, max]
        sql = (
            "UPDATE cache_entries "
            "SET ttl_expires_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', "
            "(? + (abs(random()) % ?)) || ' hours') "
            "WHERE ttl_expires_at IS NULL"
        )

        def _u(conn: sqlite3.Connection) -> int:
            cur = conn.execute(sql, (min_hours, span))
            return cur.rowcount

        return await self._run(_u)

    async def daily_summary(
        self, from_date: str | None = None, to_date: str | None = None
    ) -> dict:
        """Extensive day-wise metrics over [from_date, to_date] (UTC dates):
        for each date, the full ``metrics_daily`` totals (requests, hits, misses,
        synth_calls, words_served/synthesized, stitch_*, bytes_served, creates,
        deletes) AND a per-provider breakdown from ``metrics_daily_provider``.
        Returns ``{date: {"totals": {...raw sums...}, "by_provider": {prov: {...}}}}``.
        Derived rates (hit_rate, words_from_cache_pct, stitch_coverage_avg) are
        computed by the caller — this returns raw sums only."""
        clauses: list[str] = []
        args: list = []
        if from_date:
            clauses.append("date >= ?")
            args.append(from_date)
        if to_date:
            clauses.append("date <= ?")
            args.append(to_date)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        def _q(conn: sqlite3.Connection) -> dict:
            out: dict[str, dict] = {}
            for r in conn.execute(f"SELECT * FROM metrics_daily{where} ORDER BY date", args):
                d = r["date"]
                out.setdefault(d, {"totals": {}, "by_provider": {}})
                out[d]["totals"] = {
                    "requests": r["requests"], "hits": r["hits"], "misses": r["misses"],
                    "bytes_served": r["bytes_served"], "synth_calls": r["synth_calls"],
                    "base64_uploads": r["base64_uploads"], "creates": r["creates"],
                    "deletes": r["deletes"], "words_served": r["words_served"],
                    "words_synthesized": r["words_synthesized"],
                    "stitch_calls": r["stitch_calls"],
                    "stitch_words_assembled": r["stitch_words_assembled"],
                    "stitch_words_synthesized": r["stitch_words_synthesized"],
                }
            prow = (
                "SELECT date, provider, requests, hits, misses, synth_calls, "
                f"bytes_served, words_served, words_synthesized "
                f"FROM metrics_daily_provider{where} ORDER BY date, provider"
            )
            for r in conn.execute(prow, args):
                d = r["date"]
                out.setdefault(d, {"totals": {}, "by_provider": {}})
                out[d]["by_provider"][r["provider"]] = {
                    "requests": r["requests"], "hits": r["hits"], "misses": r["misses"],
                    "synth_calls": r["synth_calls"], "bytes_served": r["bytes_served"],
                    "words_served": r["words_served"], "words_synthesized": r["words_synthesized"],
                }
            return out

        return await self._run(_q)

    async def delete_where(
        self, where_sql: str, args: list, *, dry_run: bool
    ) -> list[dict]:
        """Generic filtered delete for the cache-control endpoints. SELECTs rows
        matching ``where_sql`` (always returned for preview + blob cleanup); when
        not ``dry_run``, DELETEs them and adjusts ``provider_totals`` in one
        ``BEGIN IMMEDIATE`` transaction. Returns a list of dicts
        ``{provider, size_bytes, storage_path, text, key, voice_id}`` per matched
        row. ``where_sql`` is the clause text without the leading ``WHERE``."""
        where = f" WHERE {where_sql}" if where_sql else ""
        select = (
            f"SELECT provider, size_bytes, storage_path, text, key, voice_id "
            f"FROM cache_entries{where}"
        )
        totals_sql = (
            "INSERT INTO provider_totals (provider, entries, total_bytes, total_words) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(provider) DO UPDATE SET "
            "entries = entries + excluded.entries, total_bytes = total_bytes + excluded.total_bytes, "
            "total_words = total_words + excluded.total_words"
        )
        cols = ("provider", "size_bytes", "storage_path", "text", "key", "voice_id")

        def _matched(conn: sqlite3.Connection) -> list[dict]:
            return [dict(zip(cols, r)) for r in conn.execute(select, args).fetchall()]

        def _d(conn: sqlite3.Connection) -> list[dict]:
            if dry_run:
                return _matched(conn)  # preview only — no write lock taken
            # SELECT inside the write transaction so a concurrent insert between
            # the snapshot and the DELETE can't orphan a blob or drift the totals
            # delta (TOCTOU-safe, matching delete_filtered / purge_expired).
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(select, args).fetchall()
                if rows:
                    conn.execute(f"DELETE FROM cache_entries{where}", args)
                    deltas: dict[str, list[int]] = {}
                    for r in rows:
                        d = deltas.setdefault(r[0], [0, 0, 0])
                        d[0] -= 1
                        d[1] -= r[1]
                        d[2] -= _wc(r[3])
                    for prov, (de, db, dw) in deltas.items():
                        conn.execute(totals_sql, (prov, de, db, dw))
                conn.execute("COMMIT")
                return [dict(zip(cols, r)) for r in rows]
            except BaseException:
                conn.execute("ROLLBACK")
                raise

        return await self._run(_d)

    async def adjust_totals(
        self, provider: str, delta_entries: int, delta_bytes: int, delta_words: int = 0
    ) -> None:
        sql = (
            "INSERT INTO provider_totals (provider, entries, total_bytes, total_words) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(provider) DO UPDATE SET "
            "entries = entries + excluded.entries, total_bytes = total_bytes + excluded.total_bytes, "
            "total_words = total_words + excluded.total_words"
        )

        def _w(conn: sqlite3.Connection) -> None:
            conn.execute(sql, (provider, delta_entries, delta_bytes, delta_words))

        await self._run(_w)

    async def record_metrics(self, *, provider: str | None = None, **deltas: int) -> None:
        """Upsert today's UTC daily-rollup row, adding the given metric deltas.

        When ``provider`` is given, also upsert the per-provider-relevant subset
        into metrics_daily_provider (per-provider hit rate / synth calls)."""
        if not deltas:
            return
        today = datetime.now(timezone.utc).date().isoformat()
        cols = list(deltas)
        col_list = ", ".join(cols)
        placeholders = ", ".join("?" for _ in cols)
        upd = ", ".join(f"{c} = {c} + excluded.{c}" for c in cols)
        sql = (
            f"INSERT INTO metrics_daily (date, {col_list}) VALUES (?, {placeholders}) "
            f"ON CONFLICT(date) DO UPDATE SET {upd}"
        )
        values = [today, *(deltas[c] for c in cols)]

        # Per-provider rollup (only the per-provider-relevant counters).
        psub = {c: deltas[c] for c in _PROVIDER_COLS if c in deltas}
        psql, pvals = None, None
        if provider is not None and psub:
            pcols = list(psub)
            pcol_list = ", ".join(pcols)
            pph = ", ".join("?" for _ in pcols)
            pupd = ", ".join(f"{c} = {c} + excluded.{c}" for c in pcols)
            psql = (
                f"INSERT INTO metrics_daily_provider (date, provider, {pcol_list}) "
                f"VALUES (?, ?, {pph}) ON CONFLICT(date, provider) DO UPDATE SET {pupd}"
            )
            pvals = [today, provider, *(psub[c] for c in pcols)]

        def _w(conn: sqlite3.Connection) -> None:
            conn.execute(sql, values)
            if psql is not None:
                conn.execute(psql, pvals)

        await self._run(_w)

    async def touch_and_record(
        self, key: str, metric_deltas: dict, *, provider: str | None = None
    ) -> None:
        """Increment an entry's hit_count/last_accessed AND daily metrics in one hop.

        When ``provider`` is given, also upsert the per-provider subset."""
        today = datetime.now(timezone.utc).date().isoformat()
        cols = list(metric_deltas)
        col_list = ", ".join(cols)
        placeholders = ", ".join("?" for _ in cols)
        upd = ", ".join(f"{c} = {c} + excluded.{c}" for c in cols)
        msql = (
            f"INSERT INTO metrics_daily (date, {col_list}) VALUES (?, {placeholders}) "
            f"ON CONFLICT(date) DO UPDATE SET {upd}"
        )
        mvals = [today, *(metric_deltas[c] for c in cols)]

        psub = {c: metric_deltas[c] for c in _PROVIDER_COLS if c in metric_deltas}
        psql, pvals = None, None
        if provider is not None and psub:
            pcols = list(psub)
            pcol_list = ", ".join(pcols)
            pph = ", ".join("?" for _ in pcols)
            pupd = ", ".join(f"{c} = {c} + excluded.{c}" for c in pcols)
            psql = (
                f"INSERT INTO metrics_daily_provider (date, provider, {pcol_list}) "
                f"VALUES (?, ?, {pph}) ON CONFLICT(date, provider) DO UPDATE SET {pupd}"
            )
            pvals = [today, provider, *(psub[c] for c in pcols)]

        def _t(conn: sqlite3.Connection) -> None:
            # Bump hit_count by the ``hits`` delta (default 1) so batched writes
            # (write-behind sums N hits into one call) advance the row by N, not 1.
            hits_delta = int(metric_deltas.get("hits", 1))
            conn.execute(
                "UPDATE cache_entries SET hit_count = hit_count + ?, last_accessed_at = ? "
                "WHERE key = ?",
                (hits_delta, _now(), key),
            )
            conn.execute(msql, mvals)
            if psql is not None:
                conn.execute(psql, pvals)

        await self._run(_t)

    async def record_latency_batch(self, samples: list[tuple]) -> None:
        """Persist a batch of ``(kind, latency_us, provider)`` latency samples (today).

        ``provider`` is nullable for back-compat (older call sites may pass None)."""
        if not samples:
            return
        today = datetime.now(timezone.utc).date().isoformat()
        rows = [(today, kind, int(us), prov) for kind, us, prov in samples]

        def _w(conn: sqlite3.Connection) -> None:
            conn.executemany(
                "INSERT INTO latency_samples (date, kind, latency_us, provider) "
                "VALUES (?, ?, ?, ?)",
                rows,
            )

        await self._run(_w)

    async def record_latency(self, kind: str, latency_us: int, provider: str | None = None) -> None:
        """Single-sample convenience (used when write-behind is disabled)."""
        await self.record_latency_batch([(kind, latency_us, provider)])

    async def metrics_summary(self, from_date: str | None = None, to_date: str | None = None) -> dict:
        clauses: list[str] = []
        args: list = []
        if from_date:
            clauses.append("date >= ?")
            args.append(from_date)
        if to_date:
            clauses.append("date <= ?")
            args.append(to_date)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        def _q(conn: sqlite3.Connection) -> dict:
            r = conn.execute(
                "SELECT COALESCE(SUM(requests),0), COALESCE(SUM(hits),0), "
                "COALESCE(SUM(misses),0), COALESCE(SUM(bytes_served),0), "
                "COALESCE(SUM(synth_calls),0), COALESCE(SUM(base64_uploads),0), "
                "COALESCE(SUM(creates),0), COALESCE(SUM(deletes),0), "
                "COALESCE(SUM(words_served),0), COALESCE(SUM(words_synthesized),0), "
                "COALESCE(SUM(stitch_calls),0), COALESCE(SUM(stitch_words_assembled),0), "
                "COALESCE(SUM(stitch_words_synthesized),0) "
                f"FROM metrics_daily{where}",
                args,
            ).fetchone()
            (requests, hits, misses, bytes_served, synth_calls, base64_uploads, creates,
             deletes, words_served, words_synthesized, stitch_calls, stitch_words_assembled,
             stitch_words_synthesized) = r
            assembled_total = stitch_words_assembled + stitch_words_synthesized
            return {
                "requests": requests,
                "hits": hits,
                "misses": misses,
                "hit_rate": round(hits / requests, 4) if requests else None,
                "bytes_served": bytes_served,
                "synth_calls": synth_calls,
                "base64_uploads": base64_uploads,
                "creates": creates,
                "deletes": deletes,
                "words_served": words_served,
                "words_synthesized": words_synthesized,
                "stitch_calls": stitch_calls,
                "stitch_words_assembled": stitch_words_assembled,
                "stitch_words_synthesized": stitch_words_synthesized,
                "stitch_coverage_avg": (
                    round(stitch_words_assembled / assembled_total, 4) if assembled_total else None
                ),
            }

        return await self._run(_q)

    async def provider_metrics_summary(
        self, from_date: str | None = None, to_date: str | None = None
    ) -> dict:
        """Per-provider daily rollup: {provider: {requests, hits, misses,
        synth_calls, bytes_served, words_served, words_synthesized, hit_rate}}
        (day-filtered)."""
        clauses: list[str] = []
        args: list = []
        if from_date:
            clauses.append("date >= ?")
            args.append(from_date)
        if to_date:
            clauses.append("date <= ?")
            args.append(to_date)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        def _q(conn: sqlite3.Connection) -> dict:
            rows = conn.execute(
                "SELECT provider, COALESCE(SUM(requests),0), COALESCE(SUM(hits),0), "
                "COALESCE(SUM(misses),0), COALESCE(SUM(synth_calls),0), "
                "COALESCE(SUM(bytes_served),0), COALESCE(SUM(words_served),0), "
                "COALESCE(SUM(words_synthesized),0) "
                f"FROM metrics_daily_provider{where} GROUP BY provider",
                args,
            ).fetchall()
            out: dict[str, dict] = {}
            for r in rows:
                prov, requests, hits, misses, synth_calls, bytes_served, words_served, words_synthesized = r
                out[prov] = {
                    "requests": requests,
                    "hits": hits,
                    "misses": misses,
                    "synth_calls": synth_calls,
                    "bytes_served": bytes_served,
                    "words_served": words_served,
                    "words_synthesized": words_synthesized,
                    "hit_rate": round(hits / requests, 4) if requests else None,
                }
            return out

        return await self._run(_q)

    async def claim_slack_summary(self, today: str) -> bool:
        """Atomically claim today's daily-summary slot. Returns True if this
        caller wins (first to post today); False if another worker already
        claimed it. The claim key is the UTC date string, so it's once-per-day.
        On a Slack send failure the caller should :meth:`release_slack_summary`
        so a later tick retries.

        Safe across the N uvicorn workers via ``BEGIN IMMEDIATE``: the first
        worker to commit the UPDATE wins (rowcount 1); concurrent callers then
        see ``value == today`` and get rowcount 0.
        """
        def _c(conn: sqlite3.Connection) -> bool:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO slack_state(key, value) "
                    "VALUES('summary_last_post', '')"
                )
                cur = conn.execute(
                    "UPDATE slack_state SET value=? "
                    "WHERE key='summary_last_post' AND value<?",
                    (today, today),
                )
                conn.execute("COMMIT")
                return cur.rowcount == 1
            except BaseException:
                conn.execute("ROLLBACK")
                raise

        return await self._run(_c)

    async def release_slack_summary(self) -> None:
        """Clear today's claim (e.g. after a Slack send failure) so a later tick
        retries. Idempotent."""
        def _r(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE slack_state SET value='' WHERE key='summary_last_post'"
            )
        await self._run(_r)

    async def latency_summary(
        self, from_date: str | None = None, to_date: str | None = None
    ) -> dict:
        """Per-kind latency {kind: {avg_us, p95_us, count}} (day-filtered).
        p95 via ORDER BY + OFFSET (SQLite has no PERCENTILE)."""
        kinds = ("ttfb", "synth", "cache_serve", "total")

        def _q(conn: sqlite3.Connection) -> dict:
            out: dict[str, dict] = {}
            for kind in kinds:
                clauses = ["kind = ?"]
                ca: list = [kind]
                if from_date:
                    clauses.append("date >= ?")
                    ca.append(from_date)
                if to_date:
                    clauses.append("date <= ?")
                    ca.append(to_date)
                where = " WHERE " + " AND ".join(clauses)
                cnt, avg = conn.execute(
                    f"SELECT COUNT(*), COALESCE(AVG(latency_us), 0) "
                    f"FROM latency_samples{where}",
                    ca,
                ).fetchone()
                if not cnt:
                    out[kind] = {"avg_us": None, "p95_us": None, "count": 0}
                    continue
                offset = max(0, min(cnt - 1, int(cnt * 0.95)))
                p95 = conn.execute(
                    f"SELECT latency_us FROM latency_samples{where} "
                    f"ORDER BY latency_us LIMIT 1 OFFSET ?",
                    [*ca, offset],
                ).fetchone()[0]
                out[kind] = {"avg_us": round(avg, 1), "p95_us": int(p95), "count": cnt}
            return out

        return await self._run(_q)

    async def latency_summary_daily(
        self, from_date: str | None = None, to_date: str | None = None
    ) -> dict:
        """Per-day, per-kind latency ``{date: {kind: {avg_us, p95_us, count}}}``.
        p95 via ORDER BY + OFFSET (matches :meth:`latency_summary`); one ``_run``
        walks the distinct sampled dates in range. Powers the per-day ``latency``
        block on /stats/daily."""
        kinds = ("ttfb", "synth", "cache_serve", "total")
        clauses: list[str] = []
        args: list = []
        if from_date:
            clauses.append("date >= ?")
            args.append(from_date)
        if to_date:
            clauses.append("date <= ?")
            args.append(to_date)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        def _q(conn: sqlite3.Connection) -> dict:
            out: dict[str, dict] = {}
            dates = [
                r[0] for r in conn.execute(
                    f"SELECT DISTINCT date FROM latency_samples{where} ORDER BY date", args
                )
            ]
            for d in dates:
                present = {
                    kind: (cnt, avg)
                    for kind, cnt, avg in conn.execute(
                        "SELECT kind, COUNT(*), COALESCE(AVG(latency_us), 0) "
                        "FROM latency_samples WHERE date = ? GROUP BY kind",
                        (d,),
                    )
                }
                d_out: dict[str, dict] = {}
                for kind in kinds:
                    if kind not in present:
                        d_out[kind] = {"avg_us": None, "p95_us": None, "count": 0}
                        continue
                    cnt, avg = present[kind]
                    offset = max(0, min(cnt - 1, int(cnt * 0.95)))
                    p95 = conn.execute(
                        "SELECT latency_us FROM latency_samples "
                        "WHERE date = ? AND kind = ? ORDER BY latency_us LIMIT 1 OFFSET ?",
                        (d, kind, offset),
                    ).fetchone()[0]
                    d_out[kind] = {"avg_us": round(avg, 1), "p95_us": int(p95), "count": cnt}
                out[d] = d_out
            return out

        return await self._run(_q)

    async def latency_summary_by_provider(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
        provider: str | None = None,
    ) -> dict:
        """Per-provider x per-day x per-kind latency
        ``{provider: {date: {kind: {avg_us, p95_us, count}}}}``.

        p95 via ORDER BY + OFFSET (matches :meth:`latency_summary_daily`); uses
        idx_latency_provider_kind_date. Pre-migration rows have NULL provider and
        are excluded (``provider IS NOT NULL`` when no filter is given)."""
        kinds = ("ttfb", "synth", "cache_serve", "total")
        clauses: list[str] = ["provider IS NOT NULL"]
        args: list = []
        if provider is not None:
            clauses.append("provider = ?")
            args.append(provider)
        if from_date:
            clauses.append("date >= ?")
            args.append(from_date)
        if to_date:
            clauses.append("date <= ?")
            args.append(to_date)
        base_where = " WHERE " + " AND ".join(clauses)

        def _q(conn: sqlite3.Connection) -> dict:
            out: dict[str, dict] = {}
            rows = conn.execute(
                f"SELECT DISTINCT provider, date FROM latency_samples{base_where} "
                f"ORDER BY provider, date",
                args,
            ).fetchall()
            for prov, d in rows:
                p_out = out.setdefault(prov, {})
                present = {
                    kind: (cnt, avg)
                    for kind, cnt, avg in conn.execute(
                        "SELECT kind, COUNT(*), COALESCE(AVG(latency_us), 0) "
                        "FROM latency_samples WHERE provider = ? AND date = ? GROUP BY kind",
                        (prov, d),
                    )
                }
                d_out: dict[str, dict] = {}
                for kind in kinds:
                    if kind not in present:
                        d_out[kind] = {"avg_us": None, "p95_us": None, "count": 0}
                        continue
                    cnt, avg = present[kind]
                    offset = max(0, min(cnt - 1, int(cnt * 0.95)))
                    p95 = conn.execute(
                        "SELECT latency_us FROM latency_samples "
                        "WHERE provider = ? AND date = ? AND kind = ? "
                        "ORDER BY latency_us LIMIT 1 OFFSET ?",
                        (prov, d, kind, offset),
                    ).fetchone()[0]
                    d_out[kind] = {"avg_us": round(avg, 1), "p95_us": int(p95), "count": cnt}
                p_out[d] = d_out
            return out

        return await self._run(_q)

    async def prune_latency(self, retention_days: int) -> None:
        """Delete latency_samples older than ``retention_days`` (called by the
        periodic checkpoint loop to bound table growth)."""
        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=retention_days)).isoformat()

        def _w(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM latency_samples WHERE date < ?", (cutoff,))

        await self._run(_w)

    async def stats(self) -> dict:
        """Cache snapshot from incrementally-maintained provider_totals (O(providers))."""

        def _q(conn: sqlite3.Connection) -> dict:
            rows = conn.execute(
                "SELECT provider, entries, total_bytes, total_words FROM provider_totals"
            ).fetchall()
            by_provider = {
                r[0]: {"entries": r[1], "total_bytes": r[2], "total_words": r[3]} for r in rows
            }
            return {
                "entries": sum(p["entries"] for p in by_provider.values()),
                "total_bytes": sum(p["total_bytes"] for p in by_provider.values()),
                "total_words": sum(p["total_words"] for p in by_provider.values()),
                "by_provider": by_provider,
            }

        return await self._run(_q)
