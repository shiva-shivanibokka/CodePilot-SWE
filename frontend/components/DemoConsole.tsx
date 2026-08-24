"use client";

import { useEffect, useMemo, useRef, useState } from "react";
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
  const [showDiff, setShowDiff] = useState(false);
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
    setShowDiff(false);
    setError("");
  }

  function push(line: LogLine) {
    setLines((prev) => [...prev, line]);
  }

  function handleEvent(e: AgentEvent) {
    const turn = e.turn ?? 0;
    const g = turn ? `T${turn}` : "··";
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
        push({ kind: "tool", gutter: g, body: `▸ ${d.tool_name}(${trim(String(arg), 140)})` });
        break;
      }
      case "tool_result":
        push({ kind: "result", gutter: "", body: `  └ ${trim(String(d.result ?? ""), 220)}` });
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
          body:
            `● agent stopped: ${d.stop_reason} · ${d.turns ?? "?"} turns · ` +
            `${d.diff_lines ?? "?"} lines changed`,
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
    <section className="section wrap" id="demo">
      <p className="eyebrow">Recorded run · the signature</p>
      <h2>Watch it work</h2>

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

      <div className="console">
        <div className="console-bar">
          <div className="dot-row">
            <span className={`dot ${running ? "live" : ""}`} />
            <span className="dot" />
            <span className="dot" />
          </div>
          <span className="console-title">swe-agent · {approach}</span>
          <span
            className="console-mode"
            style={{ color: mode === "live" ? "var(--ok)" : "var(--dim)" }}
          >
            {mode === "idle" ? "ready" : mode === "live" ? "live · your key" : "replay · recorded"}
          </span>
        </div>

        <div className="log" ref={logRef}>
          {lines.length === 0 ? (
            <div className="log-line">
              <span className="log-gutter">··</span>
              <span className="log-body muted">
                Press Play. Every line below is what the agent actually did — its own reasoning,
                its tool calls, the test output it read, and the cost as it accrued.
              </span>
            </div>
          ) : (
            lines.map((l, i) => (
              <div
                key={i}
                className={`log-line log-${
                  l.kind === "info" ? "result" : l.kind === "tool" ? "tool" : l.kind
                }`}
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

      {finished && run && (
        <div className="verdict">
          <Verdict run={run} />
          <button className="btn btn-ghost" onClick={() => setShowDiff((v) => !v)}>
            {showDiff ? "Hide the patch" : "Show the patch it wrote"}
          </button>
          {showDiff && <pre className="diff">{run.diff || "(no changes)"}</pre>}
        </div>
      )}

      {liveEnabled ? (
        <>
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
              <select
                id="provider"
                value={provider}
                onChange={(e) => setProvider(e.target.value)}
              >
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
          </div>
          <div className="byok wide">
            <div className="field">
              <label htmlFor="apikey">Your {currentProvider?.label} API key</label>
              <input
                id="apikey"
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder="used for this run only — never stored"
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
          <div className="byok-actions">
            {running ? (
              <button className="btn btn-ghost" onClick={stop}>
                Stop
              </button>
            ) : (
              <button className="btn btn-primary" onClick={runLive} disabled={!model}>
                Run live ↵
              </button>
            )}
            <button className="btn btn-ghost" onClick={runReplay} disabled={running || !run}>
              Replay the recorded run
            </button>
            {error && <span className="notice err">{error}</span>}
          </div>
          <p className="privacy">
            🔒 Your key goes straight to your chosen provider for this run and is never stored,
            logged, or reused.
          </p>
        </>
      ) : (
        <div className="byok-actions">
          {running ? (
            <button className="btn btn-ghost" onClick={stop}>
              Stop
            </button>
          ) : (
            <button className="btn btn-primary" onClick={runReplay} disabled={!run}>
              Play the recorded run ↵
            </button>
          )}
          <span className="notice">
            Live BYOK runs (Anthropic · OpenAI · Google · Groq) work when you run the backend
            locally — two commands, no Docker required.
          </span>
        </div>
      )}
    </section>
  );
}

function RunHeader({ run }: { run: RecordedRun }) {
  const recorded = formatRecordedAt(run.recordedAt);
  return (
    <div className="runcard">
      <p className="runcard-lede">
        A real run, recorded{recorded && ` on ${recorded}`}. The agent was given{" "}
        <a href={run.task.url} target="_blank" rel="noreferrer" className="link">
          {run.task.id}
        </a>{" "}
        and nothing else — no hints, and none of the tests it would be graded on. It read the
        repository, edited it, and ran the suite itself. The run below took{" "}
        <strong>{formatDuration(run.durationSeconds)}</strong> and plays back in about{" "}
        {Math.round(playbackSeconds(run.events))}s: the only thing altered is how long the pauses
        last.
      </p>
      <dl className="runcard-facts">
        <Fact label="Model" value={`${run.provider}/${run.model}`} />
        <Fact label="Real duration" value={formatDuration(run.durationSeconds)} />
        <Fact label="Cost" value={`$${run.costUsd.toFixed(4)}`} />
        <Fact label="Turns" value={String(run.turns)} />
        <Fact
          label="Tokens"
          value={(run.inputTokens + run.outputTokens).toLocaleString()}
        />
        <Fact label="Sandbox" value={run.backend === "docker" ? "Docker" : "local"} />
      </dl>
    </div>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="fact">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function Verdict({ run }: { run: RecordedRun }) {
  const g = run.grading;
  if (!g?.graded) {
    return (
      <p className="verdict-line">
        <span className="pill pill-dim">not graded</span>
        {g?.reason ? ` ${g.reason}.` : " No grading step ran for this recording."}
      </p>
    );
  }
  return (
    <div>
      <p className="verdict-line">
        <span className={`pill ${g.resolved ? "pill-ok" : "pill-bad"}`}>
          {g.resolved ? "resolved" : "not resolved"}
        </span>{" "}
        {g.resolved
          ? `The patch made the ${g.failToPassCount ?? "failing"} target test(s) pass without breaking the rest.`
          : "The patch did not satisfy the benchmark's own tests. That result is recorded as it happened."}
      </p>
      {g.output && <pre className="diff">{g.output}</pre>}
    </div>
  );
}

const trim = (s: string, n: number) => (s.length > n ? s.slice(0, n) + "…" : s);

function gradingLine(run: RecordedRun): LogLine | null {
  const g = run.grading;
  if (!g?.graded) return null;
  return {
    kind: g.resolved ? "done" : "error",
    gutter: "",
    body: g.resolved
      ? `● GRADED: resolved — the benchmark's own tests pass against this patch`
      : `● GRADED: not resolved — the benchmark's own tests still fail`,
  };
}
