/**
 * Check the committed recording before the site is built.
 *
 * The recorded run is the whole demo. If it goes missing, gets truncated by a
 * bad merge, or picks up something key-shaped, the failure is silent — the
 * console just renders an empty log on the live site. Cheaper to fail the build.
 *
 * Run: node scripts/validate-run.mjs
 */

import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const runPath = join(here, "..", "public", "demo", "run.json");

const REQUIRED = [
  "recordedAt",
  "backend",
  "approach",
  "provider",
  "model",
  "task",
  "turns",
  "costUsd",
  "durationSeconds",
  "diff",
  "events",
];

// Same shapes eval/record_run.py refuses to write. Checked again here because
// the file can also be hand-edited, and this is the last gate before publish.
const SECRETS = [
  /sk-ant-[A-Za-z0-9_-]{20,}/,
  /sk-[A-Za-z0-9]{32,}/,
  /gsk_[A-Za-z0-9]{20,}/,
  /AIza[A-Za-z0-9_-]{30,}/,
  /gh[pousr]_[A-Za-z0-9]{20,}/,
];

function fail(message) {
  console.error(`validate-run: ${message}`);
  process.exit(1);
}

if (!existsSync(runPath)) {
  // Not an error: the site renders a "record one" state when no run exists.
  console.log("validate-run: no recording committed yet — skipping.");
  process.exit(0);
}

const raw = readFileSync(runPath, "utf8");

for (const pattern of SECRETS) {
  if (pattern.test(raw)) fail(`the recording contains something key-shaped (${pattern}).`);
}

let run;
try {
  run = JSON.parse(raw);
} catch (error) {
  fail(`run.json is not valid JSON: ${error.message}`);
}

const missing = REQUIRED.filter((key) => run[key] === undefined);
if (missing.length) fail(`run.json is missing: ${missing.join(", ")}`);

if (!Array.isArray(run.events) || run.events.length === 0) {
  fail("run.json has no events — the console would render an empty log.");
}

const sizeKb = Buffer.byteLength(raw) / 1024;
if (sizeKb > 4096) fail(`run.json is ${sizeKb.toFixed(0)} KB — too large to ship to a browser.`);

console.log(
  `validate-run: ok — ${run.events.length} events, ${run.turns} turns, ` +
    `$${Number(run.costUsd).toFixed(4)}, ${sizeKb.toFixed(0)} KB.`,
);
