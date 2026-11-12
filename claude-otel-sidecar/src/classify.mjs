// Classification of Claude Code `api_request*` telemetry events.
//
// Claude Code emits `api_request` when a model call completes, plus a family
// of `api_request_<reason>` events for retry and failure paths (names observed
// in Claude Code 2.1.280). NeuroLink-shaped counters need each event mapped to
// request / retry / error, plus an error_type using NeuroLink's own mapping
// (rate_limit / auth / server / client).

const RETRY_EVENTS = new Set([
  "api_request_overload_repeated",
  "api_request_fallback_triggered",
  "api_request_overload_background_dropped",
  "api_request_retry_after_too_long",
  "api_request_oauth_refresh_lock_timeout",
]);

const ERROR_EVENTS = new Set([
  "api_request_retry_exhausted",
  "api_request_non_retryable",
  "api_request_no_response_exhausted",
  "api_request_account_on_hold",
  "api_request_oauth_refresh_exhausted",
  "api_request_host_managed_auth_fail",
  "api_request_ccr_auth_exhausted",
  "api_request_aws_auth_exhausted",
  "api_request_gcp_auth_exhausted",
  "api_request_api_key_helper_auth_exhausted",
  "api_request_api_key_helper_failed",
  "api_request_wif_exchange_401",
  "api_request_dlp_denied",
  "api_request_safety_monitor_blocked",
  "api_request_verification_required",
  "api_request_last_resort_fallback",
]);

const IGNORED_EVENTS = new Set([
  "api_request_sent", // start-of-request marker; would double-count
]);

/** NeuroLink's end() error_type mapping, applied to an HTTP status. */
export function errorTypeFromStatus(status) {
  if (status === 429) return "rate_limit";
  if (status === 401 || status === 403) return "auth";
  if (status >= 500) return "server";
  return "client";
}

export function errorTypeFromEvent(eventName, status) {
  if (status !== undefined) return errorTypeFromStatus(status);
  if (/auth|verification|401/.test(eventName)) return "auth";
  if (/overload|rate/.test(eventName)) return "rate_limit";
  if (/exhausted|no_response/.test(eventName)) return "server";
  return "client";
}

/**
 * Classify one event name into a processing bucket.
 * Returns "request" | "retry" | "error" | "ignore".
 */
export function classifyEvent(eventName) {
  if (eventName === "api_request") return "request";
  if (RETRY_EVENTS.has(eventName)) return "retry";
  if (ERROR_EVENTS.has(eventName)) return "error";
  if (IGNORED_EVENTS.has(eventName)) return "ignore";
  // Unknown future api_request_* events: treat terminal-sounding ones as
  // errors, others as retries, so new Claude Code versions still count.
  if (eventName.startsWith("api_request_")) {
    return /exhausted|failed|denied|blocked|non_retryable|on_hold/.test(eventName)
      ? "error"
      : "retry";
  }
  return "other";
}
