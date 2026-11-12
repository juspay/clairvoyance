#!/usr/bin/env node
// Claude Code statusline command that ALSO ships exact plan-usage data to the
// claude-code-metrics-sidecar. Configure in ~/.claude/settings.json:
//
//   "statusLine": {
//     "type": "command",
//     "command": "node ~/.claude/cc-usage-statusline.mjs",
//     "padding": 0
//   }
//
// Reads the statusline JSON on stdin. On claude.ai subscription lanes it
// contains `rate_limits.limits[]` — the SERVER's own reading (percent 0-100
// per window). Debounced POST to the sidecar /v1/usage (token reused from
// OTEL_EXPORTER_OTLP_HEADERS), then prints a one-line status showing the
// developer their own 5h / weekly utilization.
//
// No npm dependencies. Exits fast; network POST is capped at 1.5s.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const ENDPOINT = process.env.OTEL_EXPORTER_OTLP_ENDPOINT ?? "";
const TOKEN = (() => {
  const raw = process.env.OTEL_EXPORTER_OTLP_HEADERS ?? "";
  const m = raw.match(/authorization=([^\s,]+)/i);
  return m ? decodeURIComponent(m[1]) : "";
})();

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (c) => (input += c));
process.stdin.on("end", async () => {
  let p;
  try {
    p = JSON.parse(input);
  } catch {
    process.exit(0);
  }
  const limits = p?.rate_limits?.limits;
  const model = p?.model?.display_name ?? "";

  // 1. ship usage data (debounced per session: every 30s or on >=1% change)
  let shipped = false;
  if (ENDPOINT && TOKEN && p.rate_limits && p.session_id) {
    shipped = await shipDebounced(p);
  }

  // 2. render the status line
  const parts = [];
  if (model) parts.push(model);
  const { fiveHour, sevenDay } = pick(p.rate_limits);
  if (fiveHour !== undefined) parts.push(`5h ${Math.round(fiveHour * 100)}%`);
  if (sevenDay !== undefined) parts.push(`wk ${Math.round(sevenDay * 100)}%`);
  console.log(parts.join(" · ") || "claude");
});

function pick(rateLimits) {
  // Shape A (2.1.29x+ statusline): each window INDEPENDENTLY optional — a
  // window drops once its resets_at passes, so seven_day-only payloads are
  // normal and must still render + ship.
  const fh = rateLimits?.five_hour;
  const sd = rateLimits?.seven_day;
  const fhPct = fh && typeof fh.used_percentage === "number" ? fh.used_percentage / 100 : undefined;
  const sdPct = sd && typeof sd.used_percentage === "number" ? sd.used_percentage / 100 : undefined;
  if (fhPct !== undefined || sdPct !== undefined) {
    return {
      fiveHour: fhPct,
      sevenDay: sdPct,
      reset5h: fh ? toIso(fh.resets_at) : null,
      reset7d: sd ? toIso(sd.resets_at) : null,
    };
  }
  // Shape B (limits[] rows): { limits: [{ kind, percent, resets_at }] }
  let fiveHour;
  let sevenDay;
  let reset5h = null;
  let reset7d = null;
  let sawWeeklyAll = false;
  for (const row of rateLimits?.limits ?? []) {
    if (!row || typeof row.percent !== "number" || typeof row.kind !== "string") continue;
    if (row.kind === "session" && fiveHour === undefined) {
      fiveHour = row.percent / 100;
      reset5h = toIso(row.resets_at);
    } else if (row.kind.startsWith("weekly")) {
      const v = row.percent / 100;
      if (row.kind === "weekly_all") {
        sawWeeklyAll = true;
        sevenDay = v;
        reset7d = toIso(row.resets_at);
      } else if (!sawWeeklyAll && (sevenDay === undefined || v > sevenDay)) {
        sevenDay = v;
        reset7d = toIso(row.resets_at);
      }
    }
  }
  return { fiveHour, sevenDay, reset5h, reset7d };
}

/** resets_at arrives as epoch seconds (number) or ISO string. */
function toIso(v) {
  if (v === null || v === undefined) return null;
  if (typeof v === "number") return new Date(v * 1000).toISOString();
  return String(v);
}

async function shipDebounced(p) {
  const stateFile = path.join(os.tmpdir(), `cc-usage-sl-${p.session_id}.json`);
  const { fiveHour, sevenDay } = pick(p.rate_limits);
  const now = Date.now();
  let state = {};
  try {
    state = JSON.parse(fs.readFileSync(stateFile, "utf8"));
  } catch {
    /* first run */
  }
  const changed =
    state.fh === undefined ||
    Math.abs((fiveHour ?? state.fh) - state.fh) >= 0.01 ||
    Math.abs((sevenDay ?? state.sd) - state.sd) >= 0.01;
  if (!changed && now - (state.at ?? 0) < 30_000) return false;
  try {
    fs.writeFileSync(stateFile, JSON.stringify({ fh: fiveHour, sd: sevenDay, at: now }));
  } catch {
    /* non-fatal */
  }
  try {
    const res = await fetch(`${ENDPOINT.replace(/\/+$/, "")}/v1/usage`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: TOKEN },
      body: JSON.stringify({
        session_id: p.session_id,
        version: p.version,
        rate_limits: p.rate_limits,
      }),
      signal: AbortSignal.timeout(1500),
    });
    return res.ok;
  } catch {
    return false;
  }
}
