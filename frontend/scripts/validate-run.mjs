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
const demoDir = join(here, "..", "public", "demo");
const indexPath = join(demoDir, "index.json");

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

if (!existsSync(indexPath)) {
  // Not an error: the site renders a "record one" state when none exists.
  console.log("validate-run: no recordings committed yet — skipping.");
  process.exit(0);
}

const index = JSON.parse(readFileSync(indexPath, "utf8"));
if (!Array.isArray(index.runs) || index.runs.length === 0) {
  fail("index.json lists no runs — the picker and the console would both be empty.");
}

let totalKb = 0;
const seen = new Set();
for (const entry of index.runs) {
  const path = join(demoDir, entry.file);
  if (!existsSync(path)) fail(`index.json points at ${entry.file}, which is not committed.`);

  const raw = readFileSync(path, "utf8");
  totalKb += Buffer.byteLength(raw) / 1024;

  for (const pattern of SECRETS) {
    if (pattern.test(raw)) fail(`${entry.file} contains something key-shaped (${pattern}).`);
  }

  let run;
  try {
    run = JSON.parse(raw);
  } catch (error) {
    fail(`${entry.file} is not valid JSON: ${error.message}`);
  }

  const missing = REQUIRED.filter((key) => run[key] === undefined);
  if (missing.length) fail(`${entry.file} is missing: ${missing.join(", ")}`);

  if (!Array.isArray(run.events) || run.events.length === 0) {
    fail(`${entry.file} has no events — the console would render an empty log.`);
  }
  // A manifest id carries the arm, because the same instance can be recorded
  // by both and a deep link has to address one of them.
  const expected = run.task?.id + (run.approach === "agentless" ? "-agentless" : "");
  if (expected !== entry.id) {
    fail(`${entry.file} is listed as ${entry.id} but contains ${expected}.`);
  }
  if (seen.has(entry.id)) fail(`two recordings claim the id ${entry.id}.`);
  seen.add(entry.id);
}

if (totalKb > 8192) fail(`recordings total ${totalKb.toFixed(0)} KB — too much to ship.`);

const solved = index.runs.filter((r) => r.resolved === true).length;
const graded = index.runs.filter((r) => r.resolved !== null).length;
console.log(
  `validate-run: ok — ${index.runs.length} recording(s), ${solved}/${graded} resolved, ` +
    `${totalKb.toFixed(0)} KB total.`,
);
