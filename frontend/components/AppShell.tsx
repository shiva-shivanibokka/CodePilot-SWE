"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { About } from "@/components/About";
import { Architecture } from "@/components/Architecture";
import { Benchmark } from "@/components/Benchmark";
import Rail, { type Section } from "@/components/Rail";
import RunDeck from "@/components/RunDeck";
import { RunItYourself } from "@/components/RunItYourself";
import { loadRunIndex, type RunSummary } from "@/lib/replay";

const SECTIONS: Section[] = [
  { id: "about", label: "What this is" },
  { id: "run", label: "Watch it work", note: "recorded" },
  { id: "benchmark", label: "Benchmark" },
  { id: "architecture", label: "Architecture" },
  { id: "local", label: "Run it yourself" },
];

/**
 * The whole page: rail plus one section.
 *
 * The run leads because it is the only thing here that has been measured. The
 * URL hash carries both the section and the chosen run, so a link can point at
 * a specific recording rather than at the top of a page someone has to explore.
 */
export default function AppShell() {
  const [active, setActive] = useState("run");
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [selected, setSelected] = useState<RunSummary | null>(null);
  const hydrated = useRef(false);

  useEffect(() => {
    loadRunIndex().then((index) => {
      setRuns(index);
      const [, wantedRun] = readHash();
      setSelected(index.find((r) => r.id === wantedRun) ?? index[0] ?? null);
    });
  }, []);

  // Re-reads BOTH halves of the hash, and re-runs when the index arrives.
  // Handling only the section here meant a link to a specific run worked on a
  // cold load and silently ignored the run on every hashchange after it — so
  // browser back, forward, and one deep link followed by another all left the
  // wrong recording on screen under the right URL.
  useEffect(() => {
    const apply = () => {
      const [section, runId] = readHash();
      if (SECTIONS.some((s) => s.id === section)) setActive(section);
      if (runId) setSelected((current) => runs.find((r) => r.id === runId) ?? current);
    };
    apply();
    hydrated.current = true;
    window.addEventListener("hashchange", apply);
    return () => window.removeEventListener("hashchange", apply);
  }, [runs]);

  const writeHash = useCallback((section: string, runId?: string | null) => {
    if (!hydrated.current) return;
    const hash = runId && section === "run" ? `#${section}/${runId}` : `#${section}`;
    if (window.location.hash !== hash) window.history.replaceState(null, "", hash);
  }, []);

  const selectSection = (id: string) => {
    setActive(id);
    writeHash(id, selected?.id);
  };

  const selectRun = (run: RunSummary) => {
    setSelected(run);
    writeHash("run", run.id);
  };

  return (
    <div className="app">
      <Rail
        sections={SECTIONS}
        active={active}
        onSelect={selectSection}
        run={active === "run" ? selected : null}
        runCount={runs.length}
      />

      <main className="main" id={`panel-${active}`} role="tabpanel" aria-labelledby={`tab-${active}`}>
        {active === "run" ? (
          <RunDeck runs={runs} selected={selected} onSelect={selectRun} />
        ) : (
          <div className="pane">
            {active === "about" && <About runs={runs} />}
            {active === "benchmark" && <Benchmark runs={runs} />}
            {active === "architecture" && <Architecture />}
            {active === "local" && <RunItYourself />}
          </div>
        )}
      </main>
    </div>
  );
}

/** "#run/sympy__sympy-24213" → ["run", "sympy__sympy-24213"] */
function readHash(): [string, string | null] {
  const raw = window.location.hash.replace(/^#/, "");
  const [section, ...rest] = raw.split("/");
  return [section, rest.join("/") || null];
}
