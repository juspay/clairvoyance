// Scrub real captures into stable test fixtures.
//
// JSON fixtures are scrubbed freely. Binary (protobuf) captures are scrubbed
// with SAME-LENGTH byte replacements so every field length stays valid and the
// message remains decodable.
// Usage: node scripts/make-fixtures.mjs
import fs from "node:fs";
import path from "node:path";

const TEXT_REPLACEMENTS = [
  [/narsimha\.reddy@juspay\.in/g, "dev00000@company.example"],
  [/3822d219f13f3b17d3039c6d2aa59a92d32c30434136c48cc12b2ec8b52f1867/g, "f".repeat(64)],
  [/99ce97bc-d6f5-42a4-b47a-c238cc27c306/g, "11111111-2222-3333-4444-555555555555"],
  [/ec5e5d57-3340-4e6f-acb4-3c86f305f729/g, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"],
  [/user_01WBtmnnu63LHSZCQwQvqtkG/g, "user_TESTACCOUNTID0000000000000000000"],
  [/req_[A-Za-z0-9]+/g, "req_TESTREQUESTID000000000000000000000"],
];

// [search, replacement] — must be identical byte lengths.
const BINARY_REPLACEMENTS = [
  ["narsimha.reddy@juspay.in", "dev00000@company.example"],
  [
    "3822d219f13f3b17d3039c6d2aa59a92d32c30434136c48cc12b2ec8b52f1867",
    "1".repeat(64),
  ],
  ["99ce97bc-d6f5-42a4-b47a-c238cc27c306", "11111111-2222-3333-4444-555555555555"],
  ["ec5e5d57-3340-4e6f-acb4-3c86f305f729", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"],
];

const srcDir = "fixtures/capture";
const binDir = "fixtures/capture-protobuf";
const outDir = "test/fixtures";
fs.mkdirSync(outDir, { recursive: true });

for (const dir of [srcDir, binDir]) {
  if (!fs.existsSync(dir)) continue;
  for (const f of fs.readdirSync(dir)) {
    if (f.endsWith(".json")) {
      let text = fs.readFileSync(path.join(dir, f), "utf8");
      for (const [re, to] of TEXT_REPLACEMENTS) text = text.replace(re, to);
      fs.writeFileSync(path.join(outDir, f), text);
      console.log("wrote", path.join(outDir, f));
    } else if (f.endsWith(".bin")) {
      let buf = fs.readFileSync(path.join(dir, f));
      for (const [from, to] of BINARY_REPLACEMENTS) {
        if (Buffer.byteLength(from) !== Buffer.byteLength(to)) {
          throw new Error(`binary replacement length mismatch: ${from}`);
        }
        let idx;
        while ((idx = buf.indexOf(from, idx ?? 0)) !== -1) {
          buf.write(to, idx, "utf8");
          idx += Buffer.byteLength(to);
        }
      }
      fs.writeFileSync(path.join(outDir, f), buf);
      console.log("wrote", path.join(outDir, f));
    }
  }
}
console.log("done");
