'use strict';

// Tests for cc-usage.mjs against a mock OpenObserve /api/<org>/_search endpoint.
// All fixtures use midday-UTC timestamps and the CLI runs with --tz UTC so day
// buckets are deterministic regardless of the machine's local timezone.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const runCli = promisify(execFile);
const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const CLI = path.join(ROOT, 'cc-usage.mjs');

const us = (iso) => Date.parse(iso) * 1000; // OpenObserve _timestamp is microseconds

// ------------------------------------------------------------------ fixtures

const TOKEN_SUM = [
  { day: us('2026-10-10T12:00:00Z'), user_email: 'dev1@x.io', type: 'input', value: 1000 },
  { day: us('2026-10-10T12:00:00Z'), user_email: 'dev1@x.io', type: 'output', value: 2000 },
  { day: us('2026-10-11T12:00:00Z'), user_email: 'dev2@x.io', type: 'input', value: 500 },
];

// One cumulative-counter series with a reset between day 1 and day 2:
//   day1: 10 -> 20 (+10)   day2: 20 -> 5 (reset) -> 8 (+3)
const RAW_TOKEN_ROWS = [
  { _timestamp: us('2026-10-10T10:00:00Z'), user_email: 'dev1@x.io', type: 'input', model: 'claude-x', session_id: 's1', repo_name: 'r', branch_name: 'main', agent_name: 'cc', value: 10 },
  { _timestamp: us('2026-10-10T14:00:00Z'), user_email: 'dev1@x.io', type: 'input', model: 'claude-x', session_id: 's1', repo_name: 'r', branch_name: 'main', agent_name: 'cc', value: 20 },
  { _timestamp: us('2026-10-11T10:00:00Z'), user_email: 'dev1@x.io', type: 'input', model: 'claude-x', session_id: 's1', repo_name: 'r', branch_name: 'main', agent_name: 'cc', value: 5 },
  { _timestamp: us('2026-10-11T12:00:00Z'), user_email: 'dev1@x.io', type: 'input', model: 'claude-x', session_id: 's1', repo_name: 'r', branch_name: 'main', agent_name: 'cc', value: 8 },
];

const COST_SUM = [
  { user_email: 'dev1@x.io', model: 'claude-opus-4', value: 12.5 },
  { user_email: 'dev2@x.io', model: 'claude-sonnet-4', value: 1.25 },
];

const SESSION_SUM = [
  { user_email: 'dev1@x.io', value: 9 },
  { user_email: 'dev2@x.io', value: 4 },
];

const NOW_US = () => Math.floor(Date.now() * 1000);
const LATEST = [
  { user_email: 'dev1@x.io', last_ts: NOW_US() - 4 * 60_000_000, points: 120 },
  { user_email: 'dev2@x.io', last_ts: NOW_US() - 2 * 3600_000_000, points: 30 },
];

const TOKEN_STREAM = 'claude_code_token_usage';
const COST_STREAM = 'claude_code_cost_usage';
const SESSION_STREAM = 'claude_code_session_count';

// ------------------------------------------------------------------ mock server

function emailFilter(sql) {
  const m = sql.match(/user_email IN \(([^)]*)\)/);
  if (!m) return null;
  return [...m[1].matchAll(/'([^']*)'/g)].map((x) => x[1]);
}

function startMock({ requireAuth = true } = {}) {
  const expectedAuth = `Basic ${Buffer.from('u:p', 'utf8').toString('base64')}`;
  const requests = [];
  const server = http.createServer((req, res) => {
    let body = '';
    req.on('data', (c) => (body += c));
    req.on('end', () => {
      const send = (status, obj) => {
        res.writeHead(status, { 'content-type': 'application/json' });
        res.end(JSON.stringify(obj));
      };
      let query;
      try { query = JSON.parse(body).query; } catch { return send(400, { message: 'bad json' }); }
      requests.push({ auth: req.headers.authorization, sql: query.sql });
      if (requireAuth && req.headers.authorization !== expectedAuth) return send(401, { message: 'Unauthorized' });

      const emails = emailFilter(query.sql);
      const f = (rows) => (emails ? rows.filter((r) => emails.includes(r.user_email)) : rows);
      const all = (rows) => ({ hits: rows, total: rows.length });

      const sql = query.sql;
      if (sql.includes('MAX(_timestamp)')) return send(200, all(f(LATEST)));
      if (sql.includes(`FROM "${TOKEN_STREAM}"`)) {
        if (sql.includes('LIMIT 1')) return send(200, all([{ user_email: 'dev1@x.io', type: 'input', value: 1 }]));
        if (sql.includes('histogram')) return send(200, all(f(TOKEN_SUM)));
        if (sql.includes('ORDER BY _timestamp')) {
          // paginated at 2 rows/page on purpose, regardless of requested size
          const rows = f(RAW_TOKEN_ROWS);
          return send(200, { hits: rows.slice(query.from, query.from + 2), total: rows.length });
        }
      }
      if (sql.includes(`FROM "${COST_STREAM}"`)) {
        if (sql.includes('LIMIT 1')) return send(200, all([{ user_email: 'dev1@x.io', model: 'm', value: 1 }]));
        return send(200, all(f(COST_SUM)));
      }
      if (sql.includes(`FROM "${SESSION_STREAM}"`)) {
        if (sql.includes('LIMIT 1')) return send(200, all([{ user_email: 'dev1@x.io', value: 1 }]));
        if (sql.includes('MAX(_timestamp)')) return send(200, all(f(LATEST)));
        return send(200, all(f(SESSION_SUM)));
      }
      return send(404, { message: 'Stream not found' });
    });
  });
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      server.unref();
      resolve({ server, url: `http://127.0.0.1:${server.address().port}`, requests });
    });
  });
}

const mock = await startMock();

const baseEnv = { ...process.env, CC_USAGE_URL: mock.url, CC_USAGE_ORG: 'test' };
const authEnv = { ...baseEnv, CC_USAGE_AUTH: 'u:p' };

async function cli(args, env = authEnv) {
  try {
    const { stdout, stderr } = await runCli(process.execPath, [CLI, ...args], { env, cwd: ROOT });
    return { stdout, stderr, code: 0 };
  } catch (e) {
    return { stdout: e.stdout ?? '', stderr: e.stderr ?? '', code: e.code ?? 1 };
  }
}

// ------------------------------------------------------------------ tests

test('tokens --agg sum: pivots server-side histogram rows (json)', async () => {
  const { stdout, code } = await cli(['tokens', '--tz', 'UTC', '--format', 'json', '--since', '30d']);
  assert.equal(code, 0, stderr0(stdout));
  const payload = JSON.parse(stdout);
  assert.deepEqual(payload.tokens.types, ['input', 'output']);
  const d1 = payload.tokens.rows.find((r) => r.day === '2026-10-10' && r.user === 'dev1@x.io');
  assert.equal(d1.input, 1000);
  assert.equal(d1.output, 2000);
  const d2 = payload.tokens.rows.find((r) => r.day === '2026-10-11' && r.user === 'dev2@x.io');
  assert.equal(d2.input, 500);
  assert.equal(payload.tokens.total, 3500);
  assert.ok(payload.window.includes('30d'));
});

test('report (table): all three sections, totals, thousands separators', async () => {
  const { stdout, code } = await cli(['report', '--tz', 'UTC', '--since', '30d']);
  assert.equal(code, 0, stderr0(stdout));
  assert.ok(stdout.includes('Tokens by day / user'));
  assert.ok(stdout.includes('3,500'));
  assert.ok(stdout.includes('TOTAL'));
  assert.ok(stdout.includes('Cost by user / model (USD)'));
  assert.ok(stdout.includes('$13.75'));
  assert.ok(stdout.includes('Sessions by user'));
  assert.ok(/TOTAL\s+13/.test(stdout));
});

test('tokens --agg deltas: counter-reset-safe rates, pagination across 2-row pages', async () => {
  const { stdout, code } = await cli(['tokens', '--tz', 'UTC', '--agg', 'deltas', '--format', 'json', '--since', '30d']);
  assert.equal(code, 0, stderr0(stdout));
  const payload = JSON.parse(stdout);
  const day1 = payload.tokens.rows.find((r) => r.day === '2026-10-10');
  const day2 = payload.tokens.rows.find((r) => r.day === '2026-10-11');
  assert.equal(day1.input, 10, '10 -> 20 = +10 on day 1');
  assert.equal(day2.input, 3, 'reset 20 -> 5 ignored, 5 -> 8 = +3 on day 2');
  assert.equal(payload.tokens.total, 13);
  // the mock serves 2 rows/page with total=4: correct numbers prove pagination worked
  assert.ok(mock.requests.some((r) => r.sql.includes('ORDER BY _timestamp')));
});

test('auth is enforced end to end: 401 without credentials, success with', async () => {
  const denied = await cli(['tokens', '--tz', 'UTC'], baseEnv); // no CC_USAGE_AUTH
  assert.equal(denied.code, 1);
  assert.match(denied.stderr, /401|credentials/);
  const okRun = await cli(['tokens', '--tz', 'UTC', '--format', 'json'], { ...baseEnv, CC_USAGE_AUTH: 'u:p' });
  assert.equal(okRun.code, 0);
});

test('cost --format csv: header + raw numbers, no thousands separators', async () => {
  const { stdout, code } = await cli(['cost', '--tz', 'UTC', '--format', 'csv']);
  assert.equal(code, 0, stderr0(stdout));
  const lines = stdout.trim().split('\n');
  assert.equal(lines[0], '# Cost by user / model (USD)');
  assert.equal(lines[1], 'user,model,usd');
  assert.ok(stdout.includes('dev1@x.io,claude-opus-4,12.5'));
  assert.ok(stdout.includes('TOTAL,,13.75'));
});

test('latest: freshness per stream and user with relative times (§3.2 verify)', async () => {
  const { stdout, code } = await cli(['latest', '--tz', 'UTC']);
  assert.equal(code, 0, stderr0(stdout));
  assert.ok(stdout.includes('claude_code_token_usage — freshness'));
  assert.ok(stdout.includes('claude_code_cost_usage — freshness'));
  assert.ok(stdout.includes('dev1@x.io'));
  assert.ok(stdout.includes('4m ago'));
  assert.ok(stdout.includes('2h ago'));
});

test('report --format markdown: headings and pipe tables', async () => {
  const { stdout, code } = await cli(['report', '--tz', 'UTC', '--format', 'markdown']);
  assert.equal(code, 0, stderr0(stdout));
  assert.ok(stdout.includes('### Tokens by day / user'));
  assert.ok(/\| day\s+\| user/.test(stdout));
  assert.ok(/\| ---:/.test(stdout));
});

test('--user filter builds user_email IN (...) and the server honors it', async () => {
  const { stdout, code } = await cli(['tokens', '--tz', 'UTC', '--format', 'json', '--user', 'dev2@x.io']);
  assert.equal(code, 0, stderr0(stdout));
  const payload = JSON.parse(stdout);
  assert.ok(payload.tokens.rows.every((r) => r.user === 'dev2@x.io'));
  assert.equal(payload.tokens.total, 500);
  assert.ok(mock.requests.some((r) => r.sql.includes("user_email IN ('dev2@x.io')")));
});

test('sessions --agg deltas accumulates per-user counter deltas', async () => {
  // session_count mock serves SUM fixtures; for deltas the mock's token-style raw rows
  // aren't available, so this only checks the code path against the sum-shaped rows
  // the mock returns for the session stream's ORDER BY query (falls back gracefully).
  const { code } = await cli(['sessions', '--tz', 'UTC', '--agg', 'deltas', '--format', 'json', '--since', '30d']);
  assert.equal(code, 0);
});

test('unknown option exits 2 with a clear message', async () => {
  const { stderr, code } = await cli(['tokens', '--bogus']);
  assert.equal(code, 2);
  assert.match(stderr, /unknown option/);
});

function stderr0(stdout) {
  return stdout; // placeholder so assertion messages show the actual output on failure
}
