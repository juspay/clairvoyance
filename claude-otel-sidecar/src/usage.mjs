// Exact plan-usage tracking fed by Claude Code statusline payloads.
//
// Claude Code (>= 2.1.278) includes a `rate_limits` object in the statusline
// stdin JSON on claude.ai subscription lanes, with the SERVER's own reading:
//   rate_limits.limits[] = { kind: 'session'|'weekly_all'|'weekly_scoped',
//                            percent: 0-100, resets_at: ISO, severity, ... }
// A statusline collector script POSTs these to the sidecar (/v1/usage); the
// sidecar resolves the developer account (via session_id -> user.email, which
// the api_request telemetry already provides) and stamps the latest
// utilization onto every proxy.request span it emits, using NeuroLink's exact
// attribute names so the "Account Usage %" dashboard panels work unchanged.

const STALE_MS = 2 * 60 * 60 * 1000; // don't stamp usage older than 2h
const SESSION_TTL_MS = 24 * 60 * 60 * 1000;

export class UsageStore {
  constructor() {
    /** account -> { fiveHour, sevenDay, reset5h, reset7d, updatedAt } (fractions 0..1) */
    this.byAccount = new Map();
    /** session_id -> account (learned from api_request events) */
    this.sessionIndex = new Map();
    /** session_id -> usage snapshot parked before the account was known */
    this.parked = new Map();
    this.received = 0;
    this.stamped = 0;
  }

  /** Learn session_id -> account from an api_request event (server.mjs calls this). */
  associateSession(sessionId, account) {
    if (!sessionId || !account) return;
    this.sessionIndex.set(sessionId, { account, at: Date.now() });
    // promote parked usage now that we know the account
    const parked = this.parked.get(sessionId);
    if (parked) {
      this.parked.delete(sessionId);
      this.#apply(parked, account);
    }
    // bound the index
    if (this.sessionIndex.size > 5000) this.#pruneSessions();
  }

  #pruneSessions() {
    const cutoff = Date.now() - SESSION_TTL_MS;
    for (const [k, v] of this.sessionIndex) if (v.at < cutoff) this.sessionIndex.delete(k);
  }

  /**
   * Ingest one statusline payload: { session_id?, account?, rate_limits: { limits: [...] } | null }
   */
  record(payload) {
    this.received++;
    const usage = extractUsage(payload?.rate_limits);
    if (!usage) return false;

    const account =
      payload.account ?? this.sessionIndex.get(payload.session_id ?? "")?.account ?? null;
    if (account) {
      this.#apply(usage, account);
      return true;
    }
    if (payload.session_id) {
      this.parked.set(payload.session_id, usage);
      if (this.parked.size > 1000) this.parked.delete(this.parked.keys().next().value);
    }
    return false;
  }

  #apply(usage, account) {
    const prev = this.byAccount.get(account);
    // Keep the fresher value per window: statusline can flap to null while a
    // fetch is in flight; never let a missing window erase a known one.
    this.byAccount.set(account, {
      fiveHour: usage.fiveHour ?? prev?.fiveHour,
      sevenDay: usage.sevenDay ?? prev?.sevenDay,
      reset5h: usage.reset5h ?? prev?.reset5h,
      reset7d: usage.reset7d ?? prev?.reset7d,
      updatedAt: Date.now(),
    });
  }

  /** Span-stamp view: {fiveHour, sevenDay, reset5h, reset7d} or null when unknown/stale. */
  forAccount(account) {
    const u = this.byAccount.get(account);
    if (!u || Date.now() - u.updatedAt > STALE_MS) return null;
    return u;
  }
}

/**
 * Map server usage rows to NeuroLink window semantics.
 * kind 'session' -> 5-hour window; kinds starting 'weekly' -> 7-day window
 * (prefer weekly_all; otherwise the highest weekly row, e.g. weekly_scoped).
 */
/**
 * Accept any statusline shape. Each window is INDEPENDENTLY optional per the
 * official schema: a window appears only after the first API response and
 * drops once its resets_at passes, so seven_day-only (or five_hour-only)
 * payloads are normal and must still be applied:
 *   A: { five_hour?: { used_percentage, resets_at }, seven_day?: {...} }  (2.1.29x+)
 *   B: { limits: [{ kind, percent, resets_at }] }                         (schema variant)
 */
export function extractUsage(rateLimits) {
  if (!rateLimits || typeof rateLimits !== "object") return null;
  const fh = rateLimits.five_hour;
  const sd = rateLimits.seven_day;
  const fiveHour = fh && typeof fh.used_percentage === "number" ? fh.used_percentage / 100 : undefined;
  const sevenDay = sd && typeof sd.used_percentage === "number" ? sd.used_percentage / 100 : undefined;
  if (fiveHour !== undefined || sevenDay !== undefined) {
    return {
      fiveHour,
      sevenDay,
      reset5h: fh ? toIso(fh.resets_at) : null,
      reset7d: sd ? toIso(sd.resets_at) : null,
    };
  }
  if (Array.isArray(rateLimits.limits)) return extractWindows(rateLimits.limits);
  return null;
}

const toIso = (v) =>
  v === null || v === undefined ? null : typeof v === "number" ? new Date(v * 1000).toISOString() : String(v);

export function extractWindows(limits) {
  let fiveHour;
  let reset5h = null;
  let sevenDay;
  let reset7d = null;
  let sawWeeklyAll = false;
  for (const row of limits) {
    if (!row || typeof row.percent !== "number" || typeof row.kind !== "string") continue;
    if (row.kind === "session" && fiveHour === undefined) {
      fiveHour = row.percent / 100;
      reset5h = row.resets_at ?? null;
    } else if (row.kind.startsWith("weekly")) {
      const v = row.percent / 100;
      if (row.kind === "weekly_all") {
        sawWeeklyAll = true;
        sevenDay = v;
        reset7d = row.resets_at ?? null;
      } else if (!sawWeeklyAll && (sevenDay === undefined || v > sevenDay)) {
        sevenDay = v;
        reset7d = row.resets_at ?? null;
      }
    }
  }
  if (fiveHour === undefined && sevenDay === undefined) return null;
  return { fiveHour, sevenDay, reset5h, reset7d };
}
