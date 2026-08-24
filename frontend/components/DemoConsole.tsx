"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import InfoTip from "@/components/InfoTip";
import { fetchProviders, startTask, streamEvents } from "@/lib/api";
import {
  formatDuration,
  formatRecordedAt,
  loadRecordedRun,
  playRun,
  playbackSeconds,
  type RecordedRun,
} from "@/lib/replay";
import type { AgentEvent, Approach, Provider, ProvidersResponse } from "@/lib/types";

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

export function DemoConsole() {
  const [reg, setReg] = useState<ProvidersResponse | null>(null);
  const [run, setRun] = useState<RecordedRun | null>(null);
  const [loadingRun, setLoadingRun] = useState(true);

  const [provider, setProvider] = useState("anthropic");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [issueUrl, setIssueUrl] = useState("");
  const [approach, setApproach] = useState<Approach>("agent");

  const [lines, setLines] = useState<LogLine[]>([]);
  const [cost, setCost] = useState<Cost>({ usd: 0, tokens: 0, turns: 0 });
  const [running, setRunning] = useState(false);
  const [finished, setFinished] = useState(false);
  const [mode, setMode] = useState<"idle" | "replay" | "live">("idle");
  const [error, setError] = useState("");

  const stopRef = useRef(false);
  const cleanupRef = useRef<null | (() => void)>(null);
  const logRef = useRef<HTMLDivElement>(null);

  const liveEnabled = !!reg?.live_runs_enabled;
  const currentProvider: Provider | undefined = useMemo(
    () => reg?.providers.find((p) => p.key === provider),
    [reg, provider],
  );

  useEffect(() => {
    fetchProviders().then((r) => {
      if (!r) return;
      setReg(r);
      const first = r.providers[0];
      if (first) {
        setProvider(first.key);
        setModel(first.models[0]?.id ?? "");
      }
    });
  }, []);

  useEffect(() => {
    loadRecordedRun()
      .then((r) => {
        setRun(r);
        if (r) setIssueUrl(r.task.url);
      })
      .finally(() => setLoadingRun(false));
  }, []);

  useEffect(() => {
    if (currentProvider) setModel(currentProvider.models[0]?.id ?? "");
  }, [currentProvider]);

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [lines]);

  useEffect(() => () => cleanupRef.current?.(), []);

  function reset() {
    stopRef.current = false;
    setLines([]);
    setCost({ usd: 0, tokens: 0, turns: 0 });
    setFinished(false);
    setError("");
  }

  function push(line: LogLine) {
    setLines((prev) => [...prev, line]);
  }

  function handleEvent(e: AgentEvent) {
    const turn = e.turn ?? 0;
    const g = turn ? `T${turn}` : "";
    const d = (e.data ?? {}) as Record<string, unknown>;
    if (turn) setCost((c) => ({ ...c, turns: Math.max(c.turns, turn) }));

    switch (e.type) {
      case "thought": {
        const text = String(d.text ?? "")
          .replace(/<\/?DONE>/g, "")
          .trim();
        if (text) push({ kind: "thought", gutter: g, body: text });
        break;
      }
      case "tool_call": {
        const inp = (d.input ?? {}) as Record<string, unknown>;
        const arg = inp.command ?? inp.query ?? inp.path ?? "";
        push({ kind: "tool", gutter: g, body: `▸ ${d.tool_name}(${trim(String(arg), 150)})` });
        break;
      }
      case "tool_result":
        push({ kind: "result", gutter: "", body: `└ ${trim(String(d.result ?? ""), 240)}` });
        break;
      case "cost_update":
        setCost({
          usd: Number(d.total_cost_usd ?? 0),
          tokens: Number(d.total_input_tokens ?? 0) + Number(d.total_output_tokens ?? 0),
          turns: turn,
        });
        break;
      case "context_compressed":
        push({ kind: "info", gutter: "", body: "· context compressed (near budget)" });
        break;
      case "done": {
        const c = Number(d.total_cost_usd ?? 0) || undefined;
        if (c !== undefined) setCost((prev) => ({ ...prev, usd: c }));
        push({
          kind: d.stop_reason === "done" ? "done" : "info",
          gutter: "",
          body: `● agent stopped: ${d.stop_reason} · ${d.turns ?? "?"} turns · ${d.diff_lines ?? "?"} lines changed`,
        });
        break;
      }
      case "error":
        push({ kind: "error", gutter: "", body: `✕ ${d.error ?? "error"}` });
        break;
    }
  }

  async function runReplay() {
    if (!run) return;
    reset();
    setRunning(true);
    setMode("replay");
    setApproach(run.approach);
    push({ kind: "info", gutter: "", body: `$ resolve ${run.task.id} — ${run.provider}/${run.model}` });

    await playRun(run.events, handleEvent, () => stopRef.current);

    if (!stopRef.current) {
      const verdict = gradingLine(run);
      if (verdict) push(verdict);
      setFinished(true);
    }
    setRunning(false);
  }

  async function runLive() {
    if (!apiKey.trim()) return setError("Enter your API key to run live.");
    if (!issueUrl.trim()) return setError("Enter a GitHub issue URL.");
    reset();
    setRunning(true);
    setMode("live");
    push({ kind: "info", gutter: "", body: `$ resolve ${issueUrl} — ${provider}/${model}` });
    try {
      const { websocketUrl } = await startTask({ issueUrl, approach, provider, model, apiKey });
      cleanupRef.current = streamEvents(websocketUrl, handleEvent, () => setRunning(false));
    } catch (err) {
      handleEvent({ type: "error", data: { error: (err as Error).message } });
      setRunning(false);
    }
  }

  function stop() {
    stopRef.current = true;
    cleanupRef.current?.();
    setRunning(false);
  }

  return (
    <>
      <div className="panel-head">
        <p className="eyebrow">Recorded run</p>
        <h2>Watch it work</h2>
      </div>

      {loadingRun ? (
        <p className="lede">Loading the recorded run…</p>
      ) : run ? (
        <RunHeader run={run} />
      ) : (
        <p className="lede">
          No run has been recorded yet. Record one with{" "}
          <code className="mono">python -m eval.record_run --instance pallets__flask-4992</code> and
          it appears here.
        </p>
      )}

      {run && <RunFacts run={run} />}

      <div className="hunk">
        <span>@@ agent log @@</span>
        <b>{run ? `${run.events.length} events` : "no run"}</b>
        <span>every line is what actually happened</span>
      </div>

      <div className="console">
        <div className="console-bar">
          <span className={`dot ${running ? "live" : ""}`} />
          <span className="dot" />
          <span className="dot" />
          <span className="console-title">swe-agent · {approach}</span>
          <span className="console-mode">
            {mode === "idle" ? "ready" : mode === "live" ? "live · your key" : "replay · recorded"}
          </span>
        </div>

        <div className="log" ref={logRef}>
          {lines.length === 0 ? (
            <div className="log-line log-result">
              <span className="log-gutter" />
              <span className="log-body">
                Press Play. Every line below is what the agent actually did — its own reasoning, its
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

        <div className="console-foot">
          <div className="meter">
            <b>${cost.usd.toFixed(4)}</b>
            <span>cost</span>
          </div>
          <div className="meter">
            <b>{cost.tokens.toLocaleString()}</b>
            <span>tokens</span>
          </div>
          <div className="meter">
            <b>{cost.turns}</b>
            <span>turns</span>
          </div>
        </div>
      </div>

      <div className="actions">
        {running ? (
          <button className="btn btn-ghost" onClick={stop}>
            Stop
          </button>
        ) : (
          <button className="btn btn-primary" onClick={runReplay} disabled={!run}>
            {finished ? "Play it again" : "Play the recorded run ↵"}
          </button>
        )}
        {liveEnabled && (
          <button className="btn btn-ghost" onClick={runLive} disabled={running || !model}>
            Run live with my key
          </button>
        )}
        {!liveEnabled && (
          <span className="notice">
            Live BYOK runs work when you run the backend locally — two commands, no Docker required.
          </span>
        )}
        {error && <span className="notice err">{error}</span>}
      </div>

      {finished && run && <Verdict run={run} />}
      {run && <Patch run={run} />}

      {liveEnabled && (
        <>
          <div className="hunk">
            <span>@@ live run @@</span>
            <b>bring your own key</b>
          </div>
          <div className="byok">
            <div className="field">
              <label htmlFor="issue">GitHub issue URL</label>
              <input
                id="issue"
                value={issueUrl}
                onChange={(e) => setIssueUrl(e.target.value)}
                placeholder="https://github.com/owner/repo/issues/123"
              />
            </div>
            <div className="field">
              <label htmlFor="provider">Provider</label>
              <select id="provider" value={provider} onChange={(e) => setProvider(e.target.value)}>
                {reg?.providers.map((p) => (
                  <option key={p.key} value={p.key}>
                    {p.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="model">Model</label>
              <select id="model" value={model} onChange={(e) => setModel(e.target.value)}>
                {currentProvider?.models.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="apikey">Your {currentProvider?.label} key</label>
              <input
                id="apikey"
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder="used for this run only"
                autoComplete="off"
              />
            </div>
            <div className="field">
              <label htmlFor="approach">Approach</label>
              <select
                id="approach"
                value={approach}
                onChange={(e) => setApproach(e.target.value as Approach)}
              >
                <option value="agent">Agentic (tool-use loop)</option>
                <option value="agentless">Agentless (3-phase)</option>
              </select>
            </div>
          </div>
          <p className="privacy">
            Your key goes straight to your chosen provider for this run and is never stored, logged,
            or reused.
          </p>
        </>
      )}
    </>
  );
}

function RunHeader({ run }: { run: RecordedRun }) {
  const recorded = formatRecordedAt(run.recordedAt);
  return (
    <div className="runcard">
      <p>
        A real run, recorded{recorded && ` on ${recorded}`}. The agent was handed{" "}
        <a href={run.task.url} target="_blank" rel="noreferrer" className="link">
          {run.task.id}
        </a>{" "}
        and nothing else — no hints, and none of the tests it would be graded on. It read the
        repository, edited it, and ran the suite itself. The run took{" "}
        <strong>{formatDuration(run.durationSeconds)}</strong> and plays back in about{" "}
        <strong>{Math.round(playbackSeconds(run.events))}s</strong>: the only thing altered is how
        long the pauses last.
      </p>
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
      <Fact
        label="Cost"
        tip="What the provider billed for this run, read back from the API rather than estimated from a price table."
      >
        <dd>${run.costUsd.toFixed(4)}</dd>
      </Fact>
      <Fact
        label="Turns"
        tip="One turn is a single model call plus the results of any tools it asked for. Nothing capped this run at eight — the agent decided it was finished."
      >
        <dd>{run.turns}</dd>
      </Fact>
      <Fact
        label="Tokens"
        tip="Input plus output across every turn. Input dominates because the whole conversation is re-sent each turn, which is why the context budget manager exists."
      >
        <dd>{(run.inputTokens + run.outputTokens).toLocaleString()}</dd>
      </Fact>
      <Fact
        label="Wall clock"
        tip="How long the real run took end to end, including the time spent waiting on the model and running the test suite."
      >
        <dd>{formatDuration(run.durationSeconds)}</dd>
      </Fact>
      <Fact
        label="Workspace"
        tip={
          run.backend === "docker"
            ? "A throwaway container per task: no network, non-root, resource-capped, host filesystem never mounted."
            : "A temp directory with its own virtualenv on the recording machine. No container — the backend that exists so the agent can run where Docker cannot."
        }
      >
        <dd className="small">{run.backend === "docker" ? "Docker sandbox" : "Local checkout"}</dd>
      </Fact>
      <Fact
        label="Model"
        tip="Every call is bring-your-own-key through one provider-agnostic client. The same loop runs on Anthropic, OpenAI, Google or Groq."
      >
        <dd className="small">{run.model}</dd>
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
  if (!g?.graded) {
    return (
      <div className="verdict">
        <p className="verdict-line">
          <span className="pill pill-dim">not graded</span>
          {g?.reason ? ` ${g.reason}.` : " No grading step ran for this recording."}
        </p>
      </div>
    );
  }
  return (
    <div className="verdict">
      <p className="verdict-line">
        <span className={`pill ${g.resolved ? "pill-ok" : "pill-bad"}`}>
          {g.resolved ? "resolved" : "not resolved"}
        </span>{" "}
        {g.resolved
          ? `The patch made the ${g.failToPassCount ?? "failing"} target test${g.failToPassCount === 1 ? "" : "s"} pass without breaking the ${g.passToPassCount ?? "other"} that already passed.`
          : "The patch did not satisfy the benchmark's own tests. That result is recorded as it happened."}
      </p>
      {g.output && <pre className="testout">{g.output.trim()}</pre>}
    </div>
  );
}

/**
 * The patch, rendered as a review rather than as a blob.
 *
 * It is the thing the agent actually produced, so it gets the line rails and
 * add/remove colouring a reviewer would expect instead of being dumped into a
 * grey <pre> that nobody reads.
 */
function Patch({ run }: { run: RecordedRun }) {
  const rows = useMemo(() => parseDiff(run.diff), [run.diff]);
  if (!rows.length) return null;

  const added = rows.filter((r) => r.kind === "add").length;
  const removed = rows.filter((r) => r.kind === "del").length;

  return (
    <>
      <div className="hunk">
        <span>@@ the patch it wrote @@</span>
        <b>
          +{added} −{removed}
        </b>
        <span>{run.conclusion ? trim(run.conclusion, 110) : ""}</span>
      </div>
      <div className="card">
        <div className="card-head">
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
    if (raw.startsWith("diff --git") || raw.startsWith("index ") || raw.startsWith("--- ") || raw.startsWith("+++ ")) {
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
