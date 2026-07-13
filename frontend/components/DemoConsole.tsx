"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { fetchProviders, startTask, streamEvents } from "@/lib/api";
import { SAMPLE_RUN, playRun } from "@/lib/replay";
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
  const [provider, setProvider] = useState("anthropic");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [issueUrl, setIssueUrl] = useState(SAMPLE_RUN.issueUrl);
  const [approach, setApproach] = useState<Approach>("agent");

  const [lines, setLines] = useState<LogLine[]>([]);
  const [cost, setCost] = useState<Cost>({ usd: 0, tokens: 0, turns: 0 });
  const [running, setRunning] = useState(false);
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
        const text = String(d.text ?? "").replace(/<\/?DONE>/g, "").trim();
        if (text) push({ kind: "thought", gutter: g, body: text });
        break;
      }
      case "tool_call": {
        const inp = (d.input ?? {}) as Record<string, unknown>;
        const arg = inp.command ?? inp.query ?? inp.path ?? "";
        push({ kind: "tool", gutter: g, body: `▸ ${d.tool_name}(${trim(String(arg), 120)})` });
        break;
      }
      case "tool_result":
        push({ kind: "result", gutter: "", body: `  └ ${trim(String(d.result ?? ""), 160)}` });
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
        const resolved = d.resolved !== false && d.stop_reason === "done";
        const c = Number(d.total_cost_usd ?? 0) || undefined;
        if (c !== undefined) setCost((prev) => ({ ...prev, usd: c }));
        push({
          kind: resolved ? "done" : "info",
          gutter: "",
          body: `● ${resolved ? "RESOLVED" : "stopped: " + d.stop_reason} · ${d.turns ?? "?"} turns · ${d.diff_lines ?? "?"} lines changed`,
        });
        break;
      }
      case "error":
        push({ kind: "error", gutter: "", body: `✕ ${d.error ?? "error"}` });
        break;
    }
  }

  async function runReplay() {
    reset();
    setRunning(true);
    setMode("replay");
    setApproach(SAMPLE_RUN.approach);
    setIssueUrl(SAMPLE_RUN.issueUrl);
    push({ kind: "info", gutter: "", body: `$ resolve ${SAMPLE_RUN.title} — sample recording` });
    await playRun(SAMPLE_RUN.events, handleEvent, () => stopRef.current);
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
      cleanupRef.current = streamEvents(
        websocketUrl,
        handleEvent,
        () => setRunning(false),
      );
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
      <p className="eyebrow">Live console · the signature</p>
      <h2>Watch it think</h2>
      <p className="lede">
        {liveEnabled
          ? "Point it at any GitHub issue, bring your own key, and watch the agent search, edit, run tests, and rack up cost in real time."
          : "The hosted demo replays a recorded run — live runs need a Docker sandbox, so they run locally. Every model call is bring-your-own-key: Anthropic, OpenAI, Google, or Groq."}
      </p>

      <div className="console">
        <div className="console-bar">
          <div className="dot-row">
            <span className={`dot ${running ? "live" : ""}`} />
            <span className="dot" />
            <span className="dot" />
          </div>
          <span className="console-title">swe-agent · {approach}</span>
          <span className="console-mode" style={{ color: mode === "live" ? "var(--ok)" : "var(--dim)" }}>
            {mode === "idle" ? "ready" : mode === "live" ? "live · your key" : "replay · sample"}
          </span>
        </div>

        <div className="log" ref={logRef}>
          {lines.length === 0 ? (
            <div className="log-line">
              <span className="log-gutter">··</span>
              <span className="log-body muted">Press Run to start. Events stream here — thoughts, tool calls, test results, cost.</span>
            </div>
          ) : (
            lines.map((l, i) => (
              <div key={i} className={`log-line log-${l.kind === "info" ? "result" : l.kind === "tool" ? "tool" : l.kind}`}>
                <span className="log-gutter">{l.gutter}</span>
                <span className="log-body">{l.body}</span>
              </div>
            ))
          )}
        </div>

        <div className="console-foot">
          <div className="meter"><b>${cost.usd.toFixed(4)}</b><span>cost</span></div>
          <div className="meter"><b>{cost.tokens.toLocaleString()}</b><span>tokens</span></div>
          <div className="meter"><b>{cost.turns}</b><span>turns</span></div>
        </div>
      </div>

      {liveEnabled ? (
        <>
          <div className="byok">
            <div className="field">
              <label htmlFor="issue">GitHub issue URL</label>
              <input id="issue" value={issueUrl} onChange={(e) => setIssueUrl(e.target.value)} placeholder="https://github.com/owner/repo/issues/123" />
            </div>
            <div className="field">
              <label htmlFor="provider">Provider</label>
              <select id="provider" value={provider} onChange={(e) => setProvider(e.target.value)}>
                {reg?.providers.map((p) => (
                  <option key={p.key} value={p.key}>{p.label}</option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="model">Model</label>
              <select id="model" value={model} onChange={(e) => setModel(e.target.value)}>
                {currentProvider?.models.map((m) => (
                  <option key={m.id} value={m.id}>{m.label}</option>
                ))}
              </select>
            </div>
          </div>
          <div className="byok wide">
            <div className="field">
              <label htmlFor="apikey">Your {currentProvider?.label} API key</label>
              <input id="apikey" type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="used for this run only — never stored" autoComplete="off" />
            </div>
            <div className="field">
              <label htmlFor="approach">Approach</label>
              <select id="approach" value={approach} onChange={(e) => setApproach(e.target.value as Approach)}>
                <option value="agent">Agentic (tool-use loop)</option>
                <option value="agentless">Agentless (3-phase)</option>
              </select>
            </div>
          </div>
          <div className="byok-actions">
            {running ? (
              <button className="btn btn-ghost" onClick={stop}>Stop</button>
            ) : (
              <button className="btn btn-primary" onClick={runLive} disabled={!model}>Run live ↵</button>
            )}
            <button className="btn btn-ghost" onClick={runReplay} disabled={running}>Replay sample</button>
            {error && <span className="notice err">{error}</span>}
          </div>
          <p className="privacy">🔒 Your key is sent straight to your chosen provider for this run and never stored, logged, or reused.</p>
        </>
      ) : (
        <div className="byok-actions">
          {running ? (
            <button className="btn btn-ghost" onClick={stop}>Stop</button>
          ) : (
            <button className="btn btn-primary" onClick={runReplay}>Replay sample run ↵</button>
          )}
          <span className="notice">Live BYOK runs (Anthropic · OpenAI · Google · Groq) available when you run the backend locally.</span>
        </div>
      )}
    </section>
  );
}

const trim = (s: string, n: number) => (s.length > n ? s.slice(0, n) + "…" : s);
