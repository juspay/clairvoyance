import { test } from "node:test";
import assert from "node:assert/strict";
import { classifyEvent, errorTypeFromStatus, errorTypeFromEvent } from "../src/classify.mjs";

test("api_request is a completed request", () => {
  assert.equal(classifyEvent("api_request"), "request");
});

test("overload/fallback events are retries", () => {
  assert.equal(classifyEvent("api_request_overload_repeated"), "retry");
  assert.equal(classifyEvent("api_request_fallback_triggered"), "retry");
  assert.equal(classifyEvent("api_request_retry_after_too_long"), "retry");
});

test("exhausted/auth events are errors", () => {
  for (const name of [
    "api_request_retry_exhausted",
    "api_request_non_retryable",
    "api_request_no_response_exhausted",
    "api_request_account_on_hold",
    "api_request_wif_exchange_401",
    "api_request_dlp_denied",
  ]) {
    assert.equal(classifyEvent(name), "error", name);
  }
});

test("api_request_sent is ignored to avoid double counting", () => {
  assert.equal(classifyEvent("api_request_sent"), "ignore");
});

test("unknown api_request_* events are classified conservatively", () => {
  assert.equal(classifyEvent("api_request_some_new_thing"), "retry");
  assert.equal(classifyEvent("api_request_something_failed"), "error");
});

test("error type mapping mirrors neurolink end()", () => {
  assert.equal(errorTypeFromStatus(429), "rate_limit");
  assert.equal(errorTypeFromStatus(401), "auth");
  assert.equal(errorTypeFromStatus(500), "server");
  assert.equal(errorTypeFromStatus(529), "server");
  assert.equal(errorTypeFromStatus(400), "client");
});

test("error type from event name when no status", () => {
  assert.equal(errorTypeFromEvent("api_request_aws_auth_exhausted"), "auth");
  assert.equal(errorTypeFromEvent("api_request_overload_repeated"), "rate_limit");
});
