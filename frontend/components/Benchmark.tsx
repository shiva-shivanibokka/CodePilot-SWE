"use client";

import { useEffect, useState } from "react";
import { fetchBenchmark } from "@/lib/api";
import type { BenchmarkSummary } from "@/lib/types";

type State = { status: "loading" | "empty" | "ready"; summaries: BenchmarkSummary[] };

export function Benchmark() {
  const [state, setState] = useState<State>({ status: "loading", summaries: [] });

  useEffect(() => {
    let alive = true;
    fetchBenchmark().then((s) => {
      if (!alive) return;
      setState(s && s.length ? { status: "ready", summaries: s } : { status: "empty", summaries: [] });
    });
    return () => {
      alive = false;
    };
  }, []);

  const agent = state.summaries.find((s) => s.approach === "agent");
  const agentless = state.summaries.find((s) => s.approach === "agentless");

  return (
    <section className="section wrap" id="benchmark">
      <p className="eyebrow">Results · head to head</p>
      <h2>The benchmark is the artifact</h2>
      <p className="lede" style={{ marginBottom: 32 }}>
        SWE-bench-lite is 300 real issues from scikit-learn, Django, Flask, requests and more. An
        issue counts as resolved only when the patch makes the failing tests pass without breaking
        the passing ones — the official grading.
      </p>

      {state.status === "ready" && agent && agentless ? (
        <div className="bench">
          <table>
            <thead>
              <tr>
                <th>Metric</th>
                <th className="col-agent">Agentic</th>
                <th className="col-agentless">Agentless</th>
              </tr>
            </thead>
            <tbody>
              <Row label="% Resolved" a={`${agent.resolve_rate}%`} b={`${agentless.resolve_rate}%`} />
              <Row
                label="Resolved / total"
                a={`${agent.resolved_count} / ${agent.total_instances}`}
                b={`${agentless.resolved_count} / ${agentless.total_instances}`}
              />
              <Row label="Avg cost / issue" a={usd(agent.avg_cost_usd)} b={usd(agentless.avg_cost_usd)} />
              <Row label="Total cost" a={usd(agent.total_cost_usd)} b={usd(agentless.total_cost_usd)} />
              <Row label="Avg turns" a={String(agent.avg_turns)} b="—" />
              <Row label="Model" a={agent.model} b={agentless.model} />
            </tbody>
          </table>
        </div>
      ) : (
        <div className="bench">
          <div className="bench-empty">
            {state.status === "loading"
              ? "Loading results…"
              : "No benchmark run has been published yet. The methodology and harness are ready — "}
            {state.status === "empty" && (
              <>
                run it end to end with{" "}
                <code>python -m eval.run_eval --compare --provider anthropic</code> and the numbers
                render here automatically. No results are ever fabricated.
              </>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

function Row({ label, a, b }: { label: string; a: string; b: string }) {
  return (
    <tr>
      <td className="metric">{label}</td>
      <td className="val">{a}</td>
      <td className="val">{b}</td>
    </tr>
  );
}

const usd = (n: number) => `$${n.toFixed(n < 1 ? 4 : 2)}`;
