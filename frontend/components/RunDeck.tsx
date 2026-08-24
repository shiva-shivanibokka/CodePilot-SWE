"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import InfoTip from "@/components/InfoTip";
import RunPicker from "@/components/RunPicker";
import {
  difficultyShort,
  formatDuration,
  formatRecordedAt,
  loadRun,
  playRun,
  playbackSeconds,
  type RecordedRun,
  type RunSummary,
} from "@/lib/replay";
import type { AgentEvent } from "@/lib/types";

interface LogLine {
  kind: "thought" | "tool" | "result" | "done" | "error" | "info";
  gutter: string;
  body: string;
}

interface Cost {
  usd: number;
  tokens: number;
  turns: number;
}

const EMPTY: Cost = { usd: 0, tokens: 0, turns: 0 };

/**
 * The run, split across two panes: what happened on the left, the log on the
 * right. Both are on screen at once, which is the whole reason the layout has
 * no page margins to give away.
 */
export default function RunDeck({
  runs,
  selected,
  onSelect,
}: {
  runs: RunSummary[];
  selected: RunSummary | null;
  onSelect: (run: RunSummary) => void;
}) {
  const [run, setRun] = useState<RecordedRun | null>(null);
  const [loading, setLoading] = useState(true);
  const [lines, setLines] = useState<LogLine[]>([]);
  const [cost, setCost] = useState<Cost>(EMPTY);
  const [playing, setPlaying] = useState(false);
  const [finished, setFinished] = useState(false);

  const stopRef = useRef(false);
  const logRef = useRef<HTMLDivElement>(null);

  // Loading a different run must abandon whatever is currently playing,
  // otherwise two playbacks interleave into one nonsense log.
  useEffect(() => {
    if (!selected) {
      setLoading(false);
      return;
    }
    let live = true;
    stopRef.current = true;
    setLoading(true);
    setLines([]);
    setCost(EMPTY);
    setFinished(false);
    setPlaying(false);

    loadRun(selected.file)
      .then((r) => live && setRun(r))
      .finally(() => live && setLoading(false));

    return () => {
      live = false;
    };
  }, [selected]);

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [lines]);

  useEffect(() => () => {
    stopRef.current = true;
  }, []);

  const handleEvent = useCallback((e: AgentEvent) => {
    const turn = e.turn ?? 0;
    const gutter = turn ? `T${turn}` : "";
    const d = (e.data ?? {}) as Record<string, unknown>;
    if (turn) setCost((c) => ({ ...c, turns: Math.max(c.turns, turn) }));

    switch (e.type) {
      case "thought": {
        const text = String(d.text ?? "").replace(/<\/?DONE>/g, "").trim();
        if (text) setLines((p) => [...p, { kind: "thought", gutter, body: text }]);
        break;
      }
      case "tool_call": {
        const input = (d.input ?? {}) as Record<string, unknown>;
        const arg = input.command ?? input.query ?? input.path ?? "";
        setLines((p) => [
          ...p,
          { kind: "tool", gutter, body: `▸ ${d.tool_name}(${trim(String(arg), 170)})` },
        ]);
        break;
      }
      case "tool_result":
        setLines((p) => [
          ...p,
          { kind: "result", gutter: "", body: `└ ${trim(String(d.result ?? ""), 260)}` },
        ]);
        break;
      case "cost_update":
        setCost({
          usd: Number(d.total_cost_usd ?? 0),
          tokens: Number(d.total_input_tokens ?? 0) + Number(d.total_output_tokens ?? 0),
          turns: turn,
        });
        break;
      case "context_compressed":
        setLines((p) => [
          ...p,
          { kind: "info", gutter: "", body: "· context compressed (near budget)" },
        ]);
        break;
      case "done": {
        const total = Number(d.total_cost_usd ?? 0) || undefined;
        if (total !== undefined) setCost((c) => ({ ...c, usd: total }));
        setLines((p) => [
          ...p,
          {
            kind: d.stop_reason === "done" ? "done" : "info",
            gutter: "",
            body: `● agent stopped: ${d.stop_reason} · ${d.turns ?? "?"} turns · ${d.diff_lines ?? "?"} lines changed`,
          },
        ]);
        break;
      }
      case "error":
        setLines((p) => [...p, { kind: "error", gutter: "", body: `✕ ${d.error ?? "error"}` }]);
        break;
    }
  }, []);

  async function play() {
    if (!run) return;
    stopRef.current = false;
    setLines([{ kind: "info", gutter: "", body: `$ resolve ${run.task.id} — ${run.provider}/${run.model}` }]);
    setCost(EMPTY);
    setFinished(false);
    setPlaying(true);

    await playRun(run.events, handleEvent, () => stopRef.current);

    if (!stopRef.current) {
      const verdict = gradingLine(run);
      if (verdict) setLines((p) => [...p, verdict]);
      setFinished(true);
    }
    setPlaying(false);
  }

  const label = selected ? difficultyShort(selected.difficulty) : "";

  return (
    <div className="split">
      <div className="work">
        <p className="eyebrow">Recorded run · {runs.length} of 300 attempted</p>
        <h1>
          Two ways to fix a bug. One agent, <em>one pipeline</em>.
        </h1>

        {loading ? (
          <p className="lede">Loading the recording…</p>
        ) : run ? (
          <p className="lede">
            The agent was handed{" "}
            <a href={run.task.url} target="_blank" rel="noreferrer" className="link">
              {run.task.id}
            </a>{" "}
            and nothing else — no hints, and none of the tests it would be graded on. It read the
            repository, edited it, and ran the suite itself.{" "}
            {run.task.difficulty && (
              <>
                The benchmark&apos;s own annotators rated this one <strong>{label}</strong> (
                {run.task.difficulty} for an engineer).{" "}
              </>
            )}
            It took <strong>{formatDuration(run.durationSeconds)}</strong> and plays back in about{" "}
            <strong>{Math.round(playbackSeconds(run.events))}s</strong>.
          </p>
        ) : (
          <p className="lede">
            No run has been recorded yet. Record one with{" "}
            <code className="mono">python -m eval.record_run --instance pallets__flask-4992</code>.
          </p>
        )}

        <RunPicker runs={runs} activeId={selected?.id ?? null} onSelect={onSelect} disabled={playing} />

        {run && <RunFacts run={run} />}

        <div className="actions">
          {playing ? (
            <button className="btn btn-ghost" onClick={() => (stopRef.current = true)}>
              Stop
            </button>
          ) : (
            <button className="btn btn-primary" onClick={play} disabled={!run}>
              {finished ? "Play it again" : "Play this run ↵"}
            </button>
          )}
          <span className="notice">
            Live runs work locally — two commands, no Docker required.
          </span>
        </div>

        {finished && run && <Verdict run={run} />}
        {run && <Patch run={run} />}
      </div>

      <div className="output">
        <div className="out-head">
          <span className={`dot ${playing ? "live" : ""}`} />
          <span className="dot" />
          <span className="dot" />
          <span className="name">swe-agent · {run?.approach ?? "agent"}</span>
          <span className="right">{playing ? "replaying" : "replay · recorded"}</span>
        </div>

        <div className="log" ref={logRef}>
          {lines.length === 0 ? (
            <div className="log-line log-result">
              <span className="log-gutter" />
              <span className="log-body">
                Press Play. Every line here is what the agent actually did — its own reasoning, its
                tool calls, the test output it read, and the cost as it accrued.
              </span>
            </div>
          ) : (
            lines.map((l, i) => (
              <div
                key={i}
                className={`log-line log-${l.kind === "info" ? "result" : l.kind === "tool" ? "tool" : l.kind}`}
              >
                <span className="log-gutter">{l.gutter}</span>
                <span className="log-body">{l.body}</span>
              </div>
            ))
          )}
        </div>

        <div className="out-foot">
          <span className="meter">
            <b>${cost.usd.toFixed(4)}</b>cost
          </span>
          <span className="meter">
            <b>{cost.tokens.toLocaleString()}</b>tokens
          </span>
          <span className="meter">
            <b>{cost.turns}</b>turns
          </span>
        </div>
      </div>
    </div>
  );
}

function RunFacts({ run }: { run: RecordedRun }) {
  const resolved = run.grading?.graded && run.grading.resolved;
  return (
    <dl className="facts">
      <Fact
        label="Verdict"
        tip="Graded the way SWE-bench does: the instance's own test patch is applied only after the agent has finished, then its FAIL_TO_PASS and PASS_TO_PASS tests are run. The agent never sees the tests it is judged on."
      >
        <dd className={resolved ? "ok" : run.grading?.graded ? "bad" : ""}>
          {run.grading?.graded ? (resolved ? "Resolved" : "Not resolved") : "Not graded"}
        </dd>
      </Fact>
      <Fact label="Cost" tip="What the provider billed for this run, read back from the API rather than estimated from a price table.">
        <dd>${run.costUsd.toFixed(4)}</dd>
      </Fact>
      <Fact label="Turns" tip="One turn is a single model call plus the results of any tools it asked for. The agent decides when it is finished; the cap only stops runaways.">
        <dd>{run.turns}</dd>
      </Fact>
      <Fact label="Tokens" tip="Input plus output across every turn. Input dominates because the whole conversation is re-sent each turn, which is why the context budget manager exists.">
        <dd>{(run.inputTokens + run.outputTokens).toLocaleString()}</dd>
      </Fact>
      <Fact label="Wall clock" tip="How long the real run took end to end, including waiting on the model and running the test suite.">
        <dd>{formatDuration(run.durationSeconds)}</dd>
      </Fact>
      <Fact
        label="Recorded"
        tip="Recordings are committed to the repository, so this page needs no backend and cannot drift from what actually happened."
      >
        <dd className="small">{formatRecordedAt(run.recordedAt)}</dd>
      </Fact>
    </dl>
  );
}

function Fact({ label, tip, children }: { label: string; tip: string; children: React.ReactNode }) {
  return (
    <div className="fact">
      <dt>
        {label}
        <InfoTip text={tip} label={label} />
      </dt>
      {children}
    </div>
  );
}

function Verdict({ run }: { run: RecordedRun }) {
  const g = run.grading;
  const wrote = run.diff.split("\n").filter((l) => /^[+-][^+-]/.test(l)).length;
  const gold = run.task.goldPatchLines;
  if (!g?.graded) {
    return (
      <div className="verdict">
        <p>
          <span className="badge badge-dim">not graded</span>
          {g?.reason ? ` ${g.reason}.` : " No grading step ran for this recording."}
        </p>
      </div>
    );
  }
  return (
    <div className="verdict">
      <p>
        <span className={`badge ${g.resolved ? "badge-ok" : "badge-bad"}`}>
          {g.resolved ? "resolved" : "not resolved"}
        </span>{" "}
        {g.resolved
          ? `The patch made the ${g.failToPassCount ?? "failing"} target test${g.failToPassCount === 1 ? "" : "s"} pass without breaking the ${g.passToPassCount ?? "other"} that already passed.`
          : "The patch did not satisfy the benchmark's own tests. Recorded as it happened — this is where the agent's limit is, and hiding it would make every other number here worth less."}
      </p>
      {!g.resolved && gold ? (
        <p>
          The agent changed <strong>{wrote}</strong> lines; the fix the maintainers actually
          shipped changed <strong>{gold}</strong>. That gap is usually the whole story on a hard
          issue: the agent solves the problem as the issue describes it, while the accepted fix
          solves a larger one the issue never mentions.
        </p>
      ) : null}
      {g.output && <pre className="testout">{g.output.trim()}</pre>}
    </div>
  );
}

/** The patch, rendered as a review rather than as a blob. */
function Patch({ run }: { run: RecordedRun }) {
  const rows = useMemo(() => parseDiff(run.diff), [run.diff]);
  if (!rows.length) {
    return (
      <>
        <div className="rule">
          <span>@@ the patch @@</span>
          <b>none</b>
          <span>the agent changed no code on this run</span>
        </div>
      </>
    );
  }

  const added = rows.filter((r) => r.kind === "add").length;
  const removed = rows.filter((r) => r.kind === "del").length;

  return (
    <>
      <div className="rule">
        <span>@@ the patch it wrote @@</span>
        <b>
          +{added} −{removed}
        </b>
        {run.task.goldPatchLines ? <span>the real fix was {run.task.goldPatchLines} lines</span> : null}
      </div>
      <div className="patch">
        <div className="patch-head">
          <span>{fileOf(run.diff) ?? "patch"}</span>
          <span className="right">unified diff · as submitted</span>
        </div>
        <div className="diff">
          {rows.map((r, i) => (
            <div key={i} className={`diff-row ${r.kind}`}>
              <span className="n">{r.n ?? ""}</span>
              <span className="t">{r.text}</span>
            </div>
          ))}
        </div>
      </div>
    </>
  );
}

interface DiffRow {
  kind: "add" | "del" | "meta" | "hunkline" | "ctx";
  n: number | null;
  text: string;
}

/** Turn a unified diff into rows with new-file line numbers on the rail. */
function parseDiff(diff: string): DiffRow[] {
  const rows: DiffRow[] = [];
  let line = 0;

  for (const raw of diff.split("\n")) {
    if (
      raw.startsWith("diff --git") ||
      raw.startsWith("index ") ||
      raw.startsWith("--- ") ||
      raw.startsWith("+++ ")
    ) {
      rows.push({ kind: "meta", n: null, text: raw });
    } else if (raw.startsWith("@@")) {
      const match = /\+(\d+)/.exec(raw);
      line = match ? Number(match[1]) : line;
      rows.push({ kind: "hunkline", n: null, text: raw });
    } else if (raw.startsWith("+")) {
      rows.push({ kind: "add", n: line++, text: raw });
    } else if (raw.startsWith("-")) {
      rows.push({ kind: "del", n: null, text: raw });
    } else if (raw.length || rows.length) {
      rows.push({ kind: "ctx", n: line++, text: raw });
    }
  }
  return rows;
}

function fileOf(diff: string): string | null {
  const match = /^\+\+\+ b\/(.+)$/m.exec(diff);
  return match ? match[1] : null;
}

const trim = (s: string, n: number) => (s.length > n ? s.slice(0, n) + "…" : s);

function gradingLine(run: RecordedRun): LogLine | null {
  const g = run.grading;
  if (!g?.graded) return null;
  return {
    kind: g.resolved ? "done" : "error",
    gutter: "",
    body: g.resolved
      ? "● GRADED: resolved — the benchmark's own tests pass against this patch"
      : "● GRADED: not resolved — the benchmark's own tests still fail",
  };
}
