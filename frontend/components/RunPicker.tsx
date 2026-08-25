"use client";

import InfoTip from "@/components/InfoTip";
import {
  approachLabel,
  difficultyRank,
  difficultyShort,
  instanceOf,
  type RunSummary,
} from "@/lib/replay";

/**
 * Choose which recorded run to watch.
 *
 * The benchmark is 300 issues and these are the few that have actually been
 * run, so the picker has to make both facts legible at once: here is the spread
 * that was attempted, and here is how each one went. Difficulty comes from
 * SWE-bench Verified's human annotations, not from anything computed here.
 */
export default function RunPicker({
  runs,
  activeId,
  onSelect,
  disabled,
}: {
  runs: RunSummary[];
  activeId: string | null;
  onSelect: (run: RunSummary) => void;
  disabled: boolean;
}) {
  if (runs.length <= 1) return null;

  const resolved = runs.filter((r) => r.resolved === true).length;
  const graded = runs.filter((r) => r.resolved !== null).length;
  // The arm badge is only information when both arms are present.
  const bothArms = new Set(runs.map((r) => r.approach ?? "agent")).size > 1;

  return (
    <>
      <div className="rule">
        <span>@@ recorded runs @@</span>
        <b>
          {resolved} of {graded} resolved
        </b>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          difficulty is the benchmark&apos;s own human rating
          <InfoTip
            label="the difficulty rating"
            text="SWE-bench Verified asked engineers how long each issue would take: under 15 minutes, 15 minutes to an hour, or one to four hours. About a third of the Lite set carries one of those labels. Nothing here computes or estimates a difficulty — an instance without a label is shown as unrated."
          />
        </span>
      </div>

      <div className="picker">
        {runs.map((run) => {
          const rank = difficultyRank(run.difficulty);
          return (
            <button
              key={run.id}
              type="button"
              className="runcard"
              aria-pressed={activeId === run.id}
              disabled={disabled}
              onClick={() => onSelect(run)}
            >
              <span className="top">
                <span className={`badge ${rank ? `diff-${rank}` : "badge-dim"}`}>
                  {difficultyShort(run.difficulty)}
                </span>
                <span
                  className={`badge ${run.resolved === null ? "badge-dim" : run.resolved ? "badge-ok" : "badge-bad"}`}
                >
                  {run.resolved === null ? "—" : run.resolved ? "solved" : "failed"}
                </span>
                {bothArms && (
                  <span className="badge badge-arm">{approachLabel(run.approach)}</span>
                )}
              </span>
              <span className="name">{instanceOf(run.id)}</span>
              <span className="title">{run.title}</span>
              <span className="foot">
                <span>${run.costUsd.toFixed(3)}</span>
                <span>{run.turns} turns</span>
                <span>{Math.round(run.durationSeconds)}s</span>
              </span>
            </button>
          );
        })}
      </div>
    </>
  );
}
