import type { AgentEvent } from "./types";

/**
 * The recorded runs.
 *
 * This used to be a single hand-written SAMPLE_RUN — invented thoughts,
 * invented token counts, an invented "4 passed in 3.21s" — which streamed into
 * the console looking exactly like a real run. It is now a set of files
 * produced by `python -m eval.record_run`, each one a genuine agent run: the
 * model's own words, the tool calls it chose, the cost the provider billed, the
 * patch it wrote, and the result of running the benchmark's own tests against
 * that patch afterwards.
 *
 * Only the pacing is synthetic: long waits are clamped so a run of several
 * minutes is watchable in about half a minute.
 */
export interface RecordedRun {
  recordedAt: string;
  backend: "local" | "docker";
  approach: "agent" | "agentless";
  provider: string;
  model: string;
  task: RunTask;
  setup: { command: string; exitCode: number } | null;
  stopReason: string;
  turns: number;
  inputTokens: number;
  outputTokens: number;
  costUsd: number;
  durationSeconds: number;
  conclusion: string;
  diff: string;
  grading: Grading | null;
  events: AgentEvent[];
}

export interface RunTask {
  kind: "swebench" | "issue";
  id: string;
  title: string;
  repo?: string;
  /** SWE-bench Verified's human annotation, when the instance carries one. */
  difficulty?: string | null;
  url: string;
  repoUrl: string;
  baseCommit: string;
  text: string;
  /** Added plus removed lines, so agent and gold are compared like for like. */
  goldPatchLines?: number;
  agentPatchLines?: number;
}

export interface Grading {
  graded: boolean;
  reason?: string;
  resolved?: boolean;
  command?: string;
  exitCode?: number;
  timedOut?: boolean;
  failToPassCount?: number;
  passToPassCount?: number;
  testsRun?: number;
  output?: string;
}

/** What the picker needs before any run is loaded. */
export interface RunSummary {
  file: string;
  id: string;
  title: string;
  repo?: string;
  difficulty?: string | null;
  resolved: boolean | null;
  model: string;
  costUsd: number;
  turns: number;
  durationSeconds: number;
  events: number;
}

const DEMO_BASE = "/demo";

/** The list of recorded runs. Empty when none has been committed yet. */
export async function loadRunIndex(): Promise<RunSummary[]> {
  try {
    const res = await fetch(`${DEMO_BASE}/index.json`, { cache: "force-cache" });
    if (!res.ok) return [];
    const body = await res.json();
    return Array.isArray(body?.runs) ? (body.runs as RunSummary[]) : [];
  } catch {
    return [];
  }
}

/** One full recording, fetched only when a visitor selects it. */
export async function loadRun(file: string): Promise<RecordedRun | null> {
  try {
    const res = await fetch(`${DEMO_BASE}/${file}`, { cache: "force-cache" });
    if (!res.ok) return null;
    const run = (await res.json()) as RecordedRun;
    return Array.isArray(run?.events) && run.events.length > 0 ? run : null;
  } catch {
    return null;
  }
}

/**
 * Play a recorded run, invoking onEvent with the original inter-event pacing.
 *
 * Each event is scheduled against a wall clock rather than by sleeping for its
 * own delay in turn. Chrome throttles timers in a hidden tab, so a chain of
 * per-event sleeps stalls the moment the visitor switches away and leaves them
 * staring at a half-finished run when they come back. Measuring from the start
 * means a backlog flushes immediately instead.
 */
export async function playRun(
  events: AgentEvent[],
  onEvent: (e: AgentEvent) => void,
  shouldStop: () => boolean,
): Promise<void> {
  const start = performance.now();
  let scheduled = 0;

  for (const e of events) {
    if (shouldStop()) return;
    scheduled += e.delayMs ?? 500;
    const wait = scheduled - (performance.now() - start);
    if (wait > 0) await new Promise((r) => setTimeout(r, wait));
    if (shouldStop()) return;
    onEvent(e);
  }
}

/** How long playback takes, in seconds — the sum of its clamped delays. */
export function playbackSeconds(events: AgentEvent[]): number {
  return events.reduce((total, e) => total + (e.delayMs ?? 500), 0) / 1000;
}

/** "4m 34s" — the wall clock the run actually took, not the playback length. */
export function formatDuration(seconds: number): string {
  const mins = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  return mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
}

/** The date a run was captured, in the reader's locale. */
export function formatRecordedAt(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" });
}

/**
 * Difficulty, for display.
 *
 * The labels are SWE-bench Verified's own human annotations — how long the
 * benchmark's annotators judged each issue would take an engineer. They are not
 * derived here and not guessed: an instance without one is shown as unlabelled.
 */
export function difficultyRank(label?: string | null): 1 | 2 | 3 | 0 {
  if (!label) return 0;
  if (label.startsWith("<15")) return 1;
  if (label.startsWith("15 min")) return 2;
  return 3;
}

export function difficultyShort(label?: string | null): string {
  const rank = difficultyRank(label);
  return rank === 1 ? "easy" : rank === 2 ? "medium" : rank === 3 ? "hard" : "unrated";
}
