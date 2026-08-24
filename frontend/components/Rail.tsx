"use client";

import type { RunSummary } from "@/lib/replay";

const REPO = "https://github.com/shiva-shivanibokka/Autonomous-SWE-Agent";

export interface Section {
  id: string;
  label: string;
  note?: string;
}

/**
 * The rail: everything global to the page.
 *
 * It carries the sections and the identity of whichever run is loaded, which is
 * also what stops the layout needing page margins — the horizontal space to the
 * left is navigation rather than emptiness.
 *
 * Follows the ARIA tabs pattern with vertical orientation, so up and down move
 * between sections and only the selected one is in the page's tab order.
 */
export default function Rail({
  sections,
  active,
  onSelect,
  run,
  runCount,
}: {
  sections: Section[];
  active: string;
  onSelect: (id: string) => void;
  run: RunSummary | null;
  runCount: number;
}) {
  const onKeyDown = (e: React.KeyboardEvent) => {
    const order = sections.map((s) => s.id);
    const at = order.indexOf(active);
    let next = at;

    if (e.key === "ArrowDown") next = (at + 1) % order.length;
    else if (e.key === "ArrowUp") next = (at - 1 + order.length) % order.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = order.length - 1;
    else return;

    e.preventDefault();
    onSelect(order[next]);
    document.getElementById(`tab-${order[next]}`)?.focus();
  };

  return (
    <aside className="rail">
      <div className="mark">
        <i>+</i> swe-agent
      </div>

      <div className="grp">
        <p>Sections</p>
        <div className="nav" role="tablist" aria-orientation="vertical" aria-label="Sections" onKeyDown={onKeyDown}>
          {sections.map((s) => (
            <button
              key={s.id}
              id={`tab-${s.id}`}
              role="tab"
              type="button"
              aria-selected={active === s.id}
              aria-controls={`panel-${s.id}`}
              tabIndex={active === s.id ? 0 : -1}
              onClick={() => onSelect(s.id)}
            >
              {s.label}
              {s.note && <span className="chip">{s.note}</span>}
            </button>
          ))}
        </div>
      </div>

      {run && (
        <div className="grp">
          <p>Loaded run</p>
          <span
            className={`badge ${run.resolved === null ? "badge-dim" : run.resolved ? "badge-ok badge-dot" : "badge-bad badge-dot"}`}
          >
            {run.resolved === null ? "not graded" : run.resolved ? "resolved" : "not resolved"}
          </span>
          <div className="rail-meta" style={{ marginTop: 11 }}>
            <span>
              instance <b>{run.id.split("__").pop()}</b>
            </span>
            <span>
              repo <b>{run.repo ?? "—"}</b>
            </span>
            <span>
              model <b>{run.model}</b>
            </span>
            <span>
              cost <b>${run.costUsd.toFixed(4)}</b>
            </span>
          </div>
        </div>
      )}

      <div className="grp">
        <p>Measured</p>
        <div className="rail-meta">
          <span>
            runs recorded <b>{runCount}</b>
          </span>
          <span>
            benchmark <b>300 issues</b>
          </span>
          <span>
            full sweep <b>not run</b>
          </span>
        </div>
      </div>

      <div className="rail-foot">
        <a href={REPO} target="_blank" rel="noreferrer">
          Source on GitHub ↗
        </a>
        <span>Bring your own key · nothing stored</span>
        <span className="byline">
          Built by <strong>Shivani Bokka</strong>
        </span>
      </div>
    </aside>
  );
}
