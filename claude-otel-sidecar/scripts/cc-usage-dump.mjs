#!/usr/bin/env node
// TEMP DIAG: capture raw statusline stdin payloads to /tmp/cc-sl-raw-dump.jsonl
// Renders a minimal status line so the UI stays usable. Remove after debugging.
import fs from "node:fs";

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (c) => (input += c));
process.stdin.on("end", () => {
  try {
    fs.appendFileSync("/tmp/cc-sl-raw-dump.jsonl", input.trimEnd() + "\n");
  } catch {
    /* non-fatal */
  }
  let p;
  try {
    p = JSON.parse(input);
  } catch {
    console.log("claude");
    process.exit(0);
  }
  const model = p?.model?.display_name ?? "";
  console.log(model ? `${model}` : "claude");
});
