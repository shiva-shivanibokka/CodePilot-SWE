"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

export interface TabDef {
  id: string;
  label: string;
  note?: string;
  content: ReactNode;
}

/**
 * The page's primary navigation.
 *
 * The run is the point of this site, and it used to sit three screens below the
 * fold. As tabs it is one click from landing, and the URL hash carries the
 * choice — so a link can point straight at the recorded run rather than at the
 * top of a page someone then has to scroll.
 *
 * Follows the ARIA tabs pattern: arrow keys move between tabs, Home and End
 * jump to the ends, and only the selected tab is in the page's tab order.
 */
export default function Tabs({ tabs, initial }: { tabs: TabDef[]; initial?: string }) {
  const [active, setActive] = useState(initial ?? tabs[0].id);
  const listRef = useRef<HTMLDivElement>(null);
  const hydrated = useRef(false);

  // Read the hash once on mount, then follow back/forward.
  useEffect(() => {
    const fromHash = () => {
      const id = window.location.hash.replace(/^#/, "");
      if (tabs.some((t) => t.id === id)) setActive(id);
    };
    fromHash();
    hydrated.current = true;
    window.addEventListener("hashchange", fromHash);
    return () => window.removeEventListener("hashchange", fromHash);
  }, [tabs]);

  const select = useCallback((id: string) => {
    setActive(id);
    if (hydrated.current && window.location.hash !== `#${id}`) {
      window.history.replaceState(null, "", `#${id}`);
    }
  }, []);

  const onKeyDown = (e: React.KeyboardEvent) => {
    const order = tabs.map((t) => t.id);
    const at = order.indexOf(active);
    let next = at;

    if (e.key === "ArrowRight") next = (at + 1) % order.length;
    else if (e.key === "ArrowLeft") next = (at - 1 + order.length) % order.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = order.length - 1;
    else return;

    e.preventDefault();
    select(order[next]);
    listRef.current?.querySelector<HTMLButtonElement>(`#tab-${order[next]}`)?.focus();
  };

  return (
    <>
      <div className="tabstrip">
        <div className="wrap">
          <div className="tablist" role="tablist" aria-label="Sections" ref={listRef} onKeyDown={onKeyDown}>
            {tabs.map((tab) => (
              <button
                key={tab.id}
                id={`tab-${tab.id}`}
                className="tab"
                role="tab"
                type="button"
                aria-selected={active === tab.id}
                aria-controls={`panel-${tab.id}`}
                tabIndex={active === tab.id ? 0 : -1}
                onClick={() => select(tab.id)}
              >
                {tab.label}
                {tab.note && <span className="count">{tab.note}</span>}
              </button>
            ))}
          </div>
        </div>
      </div>

      {tabs.map((tab) =>
        active === tab.id ? (
          <div
            key={tab.id}
            id={`panel-${tab.id}`}
            role="tabpanel"
            aria-labelledby={`tab-${tab.id}`}
            tabIndex={0}
            className="panel wrap"
          >
            {tab.content}
          </div>
        ) : null,
      )}
    </>
  );
}
