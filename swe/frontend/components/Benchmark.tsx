"use client";

import { useEffect, useState } from "react";
import InfoTip from "@/components/InfoTip";
import { fetchBenchmark } from "@/lib/api";
import { difficultyRank, difficultyShort, instanceOf, type RunSummary } from "@/lib/replay";
import type { BenchmarkSummary } from "@/lib/types";

type State = { status: "loading" | "empty" | "ready"; summaries: BenchmarkSummary[] };

export function Benchmark({ runs = [] }: { runs?: RunSummary[] }) {
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

  const graded = runs.filter((r) => r.resolved !== null);
  const solved = graded.filter((r) => r.resolved);
  const spent = runs.reduce((total, r) => total + r.costUsd, 0);
  const pairs = pairByInstance(runs);

  return (
    <>
      <div>
        <p className="eyebrow">Results · head to head</p>
        <h2>The comparison is the artifact</h2>
        <p className="lede">
          SWE-bench-lite is 300 real issues from scikit-learn, Django, Flask, requests and more. An
          issue counts as resolved only when the patch makes the failing tests pass without breaking
          the ones that already passed — the official grading, not a judgement call.
        </p>
      </div>

      <div className="rule">
        <span>@@ full benchmark @@</span>
        <b>not yet run</b>
        <span>no numbers are estimated or borrowed</span>
      </div>

      {state.status === "ready" && agent && agentless ? (
        <div className="bench">
          <table>
            <thead>
              <tr>
                <th>Metric</th>
                <th>Agentic</th>
                <th>Agentless</th>
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
              <Row label="Avg turns" a={String(agent.avg_turns)} b="— (3 phases)" />
              <Row label="Model" a={agent.model} b={agentless.model} />
            </tbody>
          </table>
        </div>
      ) : (
        <div className="bench">
          <div className="bench-empty">
            {state.status === "loading" ? (
              "Loading results…"
            ) : (
              <>
                No full benchmark run has been published, so this table is empty rather than
                estimated. The harness is here and works — running it end to end takes a paid key
                and a few hours of compute. {graded.length} instance
                {graded.length === 1 ? " has" : "s have"} been run for real: see{" "}
                <a className="link" href="#run">
                  Watch it work
                </a>
                .
              </>
            )}
          </div>
        </div>
      )}

      {pairs.length > 0 && (
        <>
          <div className="rule">
            <span>@@ recorded head to head @@</span>
            <b>
              {pairs.length} instance{pairs.length === 1 ? "" : "s"}, both arms
            </b>
            <span>same issue, same model, same machine</span>
          </div>

          <div className="bench">
            <table>
              <thead>
                <tr>
                  <th>Instance</th>
                  <th>Rated</th>
                  <th>Agentic</th>
                  <th>Agentless</th>
                </tr>
              </thead>
              <tbody>
                {pairs.map(({ id, difficulty, agent, agentless }) => (
                  <tr key={id}>
                    <td className="metric">{id}</td>
                    <td className="val">{difficultyShort(difficulty)}</td>
                    <td className="val">
                      <Arm run={agent} />
                    </td>
                    <td className="val">
                      <Arm run={agentless} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <p className="lede">
            Four instances is not a resolve rate, and this table is not one. What it does show is
            the trade the two architectures actually make on the same issue: the pipeline pays for
            every candidate it samples whether or not it uses them, while the loop pays only for
            the turns it takes. Which comes out cheaper is not fixed — it depends on the issue.
          </p>
        </>
      )}

      <div className="rule">
        <span>@@ what the papers report @@</span>
        <b>theirs, not this system&apos;s</b>
      </div>

      <dl className="facts">
        <div className="fact">
          <dt>
            Anthropic, agentic
            <InfoTip
              label="Anthropic's agentic result"
              text="From Anthropic's published SWE-bench work, on SWE-bench Verified with their own harness. Quoted here as the target this architecture mirrors — it is not a result this repository has reproduced."
            />
          </dt>
          <dd>~49%</dd>
        </div>
        <div className="fact">
          <dt>
            Agentless paper
            <InfoTip
              label="the Agentless paper's result"
              text="From Xia et al., 2024, on SWE-bench Lite at roughly $0.70 per issue. Again the source paper's number, not this system's."
            />
          </dt>
          <dd>~32%</dd>
        </div>
        <div className="fact">
          <dt>
            This system
            <InfoTip
              label="this system's resolve rate"
              text="Deliberately blank. One instance has been run and graded; a resolve rate over 300 has not, and inventing one from a single success would be the exact dishonesty this page exists to avoid."
            />
          </dt>
          <dd className="small">Not measured</dd>
        </div>
        <div className="fact">
          <dt>
            Measured so far
            <InfoTip
              label="what has been measured"
              text={`${graded.length} SWE-bench-lite instance${graded.length === 1 ? "" : "s"}, each run end to end and graded against its own FAIL_TO_PASS and PASS_TO_PASS tests. Total spend: $${spent.toFixed(2)}. The other ${300 - graded.length} have not been attempted, which is why the table above is empty.`}
            />
          </dt>
          <dd className="ok">{solved.length} / {graded.length}</dd>
        </div>
      </dl>
    </>
  );
}

/**
 * One arm's outcome on one instance.
 *
 * A blank cell means that arm has not been recorded on this instance, which is
 * different from having been recorded and failed — so the two never render the
 * same way.
 */
function Arm({ run }: { run?: RunSummary }) {
  if (!run) return <span className="muted">not run</span>;
  return (
    <>
      <span className={run.resolved === null ? "" : run.resolved ? "ok" : "bad"}>
        {run.resolved === null ? "ungraded" : run.resolved ? "solved" : "failed"}
      </span>{" "}
      <span className="muted">
        ${run.costUsd.toFixed(3)} · {Math.round(run.durationSeconds)}s
      </span>
    </>
  );
}

interface Pair {
  id: string;
  difficulty?: string | null;
  agent?: RunSummary;
  agentless?: RunSummary;
}

/** Group the recordings by benchmark instance, easiest first. */
function pairByInstance(runs: RunSummary[]): Pair[] {
  const byInstance = new Map<string, Pair>();
  for (const run of runs) {
    const id = instanceOf(run.id);
    const pair = byInstance.get(id) ?? { id, difficulty: run.difficulty };
    pair[run.approach === "agentless" ? "agentless" : "agent"] = run;
    byInstance.set(id, pair);
  }
  // Only rows where both arms ran say anything about the comparison.
  return [...byInstance.values()]
    .filter((p) => p.agent && p.agentless)
    .sort((a, b) => difficultyRank(a.difficulty) - difficultyRank(b.difficulty));
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
