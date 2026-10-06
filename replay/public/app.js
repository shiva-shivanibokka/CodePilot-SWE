/* Replay viewer. Reads only what build_data.py wrote under data/.
   No framework and no build step: the whole point of this page is that it is a
   static replay, so it should also be a static site. */

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;   // textContent, never innerHTML
  return n;
};
const fmtUsd = (n) => "$" + (n || 0).toFixed(4);
const fmtSec = (n) => (n >= 60 ? `${Math.floor(n / 60)}m ${Math.round(n % 60)}s` : `${(n || 0).toFixed(1)}s`);

const ARM_LABEL = { agent: "Agentic loop", agentless: "Agentless pipeline" };
const KIND_LABEL = {
  text: "reasoning",
  tool_call: "tool call",
  tool_result: "result (summary)",
  localize: "localisation",
  sample: "repair sample",
  budget: "budget",
  error: "error",
  done: "finished",
};
// harness.py::transcript_of caps every recorded entry at this many characters.
// Entries that reach it were cut, and the page says so rather than presenting a
// truncated string as the whole thing.
const TRANSCRIPT_CAP = 300;
const runCache = new Map();

let index = null;
let openInstance = null;

initSwitcher();
init();

function initSwitcher() {
  const buttons = document.querySelectorAll("[data-set-style]");
  const sync = () => buttons.forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.setStyle === document.documentElement.dataset.style)));
  buttons.forEach((b) => b.addEventListener("click", () => {
    document.documentElement.dataset.style = b.dataset.setStyle;
    try { localStorage.setItem("replay-style", b.dataset.setStyle); } catch { /* private window */ }
    sync();
  }));
  sync();
}

async function init() {
  try {
    index = await (await fetch("data/index.json")).json();
  } catch (err) {
    $("#instances").append(el("p", "empty", "Could not load the run data."));
    return;
  }
  $("#f-model").textContent = index.model || "—";
  $("#f-seed").textContent = String(index.seed ?? "—");
  renderTotals();
  renderInstances();
}

function renderTotals() {
  const wrap = el("div", "cards");
  const maxCost = Math.max(...index.arms.map((a) => index.totals[a]?.cost_usd || 0));
  const maxCalls = Math.max(...index.arms.map((a) => index.totals[a]?.model_calls || 0));

  for (const arm of index.arms) {
    const t = index.totals[arm];
    if (!t) continue;
    const perResolution = t.resolved ? t.cost_usd / t.resolved : null;
    const card = el("div", "card");
    card.append(el("h3", null, ARM_LABEL[arm] || arm));

    const big = el("div", "big");
    big.append(document.createTextNode(String(t.resolved)));
    big.append(el("em", null, ` / ${t.runs} resolved`));
    card.append(big);

    // Ten pips: filled where that arm resolved the instance. Same order as the
    // list below, so the eye can match a pip to a row.
    const pips = el("div", "pips");
    for (const inst of index.instances) {
      pips.append(el("i", `pip${inst.arms[arm]?.resolved ? " on" : ""}`));
    }
    card.append(pips);

    card.append(bar("Cost", t.cost_usd, maxCost, fmtUsd(t.cost_usd)));
    card.append(bar("Model calls", t.model_calls, maxCalls, String(t.model_calls)));
    card.append(bar("Per resolution", perResolution || 0, maxCost,
      perResolution === null ? "—" : fmtUsd(perResolution)));
    wrap.append(card);
  }
  $("#totals").replaceChildren(wrap);
}

function bar(label, value, max, text) {
  const row = el("div", "barline");
  row.append(el("div", "lab", label));
  const track = el("div", "track");
  const fill = el("div", "fill");
  fill.style.width = `${max > 0 ? Math.max(2, (value / max) * 100) : 0}%`;
  track.append(fill);
  row.append(track);
  row.append(el("div", "val", text));
  return row;
}

// How an arm spent its recorded steps, as one stacked strip.
function strip(kinds, total) {
  const wrap = el("div", "strip");
  if (!total) return wrap;
  for (const [kind, n] of Object.entries(kinds || {})) {
    const seg = el("i", `k-${kind}`);
    seg.style.width = `${(n / total) * 100}%`;
    seg.title = `${n} × ${KIND_LABEL[kind] || kind}`;
    wrap.append(seg);
  }
  return wrap;
}

function badge(arm, info) {
  const cls = info?.resolved ? "ok" : "no";
  const label = `${ARM_LABEL[arm] || arm}: ${info?.resolved ? "resolved" : "not resolved"}`;
  return el("span", `badge ${cls}`, label);
}

function renderInstances() {
  const wrap = $("#instances");
  wrap.replaceChildren();
  for (const inst of index.instances) {
    const row = el("button", "inst");
    row.type = "button";
    row.setAttribute("aria-expanded", "false");

    const left = el("div");
    left.append(el("div", "iid", inst.instance_id));
    const agent = inst.arms.agent;
    left.append(el("div", "meta",
      `${inst.repo}${agent ? ` · ${agent.transcript_events} recorded steps · ${agent.model_calls} calls · ${fmtSec(agent.wall_seconds)}` : ""}`));
    row.append(left);

    const act = el("div");
    if (agent) {
      act.append(strip(agent.kinds, agent.transcript_events));
      act.append(el("div", "meta", `${agent.kinds?.tool_call || 0} tool calls · ${agent.changed_lines} lines changed`));
    }
    row.append(act);

    const badges = el("div", "badges");
    for (const arm of index.arms) badges.append(badge(arm, inst.arms[arm]));
    row.append(badges);
    row.append(el("div", "meta", "replay →"));

    row.addEventListener("click", () => openDetail(inst, row));
    wrap.append(row);
  }
}

async function openDetail(inst, row) {
  document.querySelectorAll(".inst").forEach((b) => b.setAttribute("aria-expanded", "false"));
  const detail = $("#detail");

  if (openInstance === inst.instance_id) {        // clicking the open row closes it
    openInstance = null;
    detail.hidden = true;
    detail.replaceChildren();
    return;
  }
  openInstance = inst.instance_id;
  row.setAttribute("aria-expanded", "true");

  detail.hidden = false;
  detail.replaceChildren(el("p", "empty", "Loading the recorded run…"));

  const runs = {};
  for (const arm of index.arms) {
    const key = `${inst.instance_id}-${arm}`;
    if (!runCache.has(key)) {
      try {
        runCache.set(key, await (await fetch(`data/run-${key}.json`)).json());
      } catch {
        runCache.set(key, null);
      }
    }
    runs[arm] = runCache.get(key);
  }

  const head = el("div", "detail-head");
  head.append(el("h2", null, inst.instance_id));
  head.append(el("p", "aside", "Both arms were given the same issue and the same repository state."));
  const arms = el("div", "arms");
  for (const arm of index.arms) arms.append(renderArm(arm, runs[arm]));

  detail.replaceChildren(head, arms);
  detail.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderArm(arm, run) {
  const box = el("div", "arm");
  const h = el("h3");
  h.append(document.createTextNode(ARM_LABEL[arm] || arm));
  if (!run) {
    h.append(el("span", "badge err", "missing"));
    box.append(h, el("p", "empty", "No recorded run for this arm."));
    return box;
  }
  h.append(el("span", `badge ${run.resolved ? "ok" : "no"}`, run.resolved ? "resolved" : "not resolved"));
  box.append(h);

  const g = run.grade || {};
  const bits = [
    `${run.model_calls} calls`,
    fmtUsd(run.cost_usd),
    fmtSec(run.wall_seconds),
    `${run.changed_lines ?? 0} changed lines`,
  ];
  if (g.f2p_total) bits.push(`fail→pass ${g.f2p_passed}/${g.f2p_total}`);
  if (g.p2p_total) bits.push(`pass→pass ${g.p2p_passed}/${g.p2p_total}`);
  if (!g.applied) bits.push("patch did not apply");
  const stopped = Array.isArray(run.stopped_by) ? run.stopped_by.join(", ") : run.stopped_by;
  if (stopped) bits.push(`stopped: ${stopped}`);
  box.append(el("div", "stats", bits.join(" · ")));

  if (g.detail) box.append(el("p", "aside", g.detail));
  if (run.selection_basis) box.append(el("p", "aside", `Selection: ${run.selection_basis}`));

  box.append(renderTape(run.transcript || []));
  box.append(renderDiff(run.diff || ""));
  return box;
}

function renderTape(events) {
  const wrap = el("div");
  if (!events.length) {
    wrap.append(el("p", "empty", "No recorded steps."));
    return wrap;
  }

  const tape = el("div", "tape");
  const nodes = events.map((ev, i) => {
    const node = el("div", "ev");
    if (ev.kind === "error" || ev.error) node.classList.add("is-error");

    const head = el("div");
    head.append(el("span", "k", KIND_LABEL[ev.kind] || String(ev.kind || "step")));
    if (ev.tool) { head.append(document.createTextNode(" ")); head.append(el("span", "tool", ev.tool)); }
    node.append(head);

    const body = bodyText(ev);
    if (body) {
      if (body.length >= TRANSCRIPT_CAP) {
        head.append(document.createTextNode(" "));
        head.append(el("span", "badge no", `cut at ${TRANSCRIPT_CAP} chars`));
      }
      const pre = el("pre", null, body);
      node.append(pre);
      // 11em of clamped text is roughly 8 lines; only offer the toggle when
      // there is plausibly more than that, so short steps stay uncluttered.
      if (body.length > 400 || body.split("\n").length > 8) {
        const more = el("button", "more", "show all");
        more.type = "button";
        more.addEventListener("click", (e) => {
          e.stopPropagation();
          const open = pre.classList.toggle("open");
          more.textContent = open ? "show less" : "show all";
        });
        node.append(more);
      }
    }
    node.dataset.i = String(i);
    tape.append(node);
    return node;
  });

  let cursor = 0;
  const counter = el("span", "counter");
  const prev = el("button", null, "‹ prev");
  const next = el("button", null, "next ›");
  prev.type = next.type = "button";

  const show = (i) => {
    cursor = Math.max(0, Math.min(events.length - 1, i));
    nodes.forEach((n, j) => n.classList.toggle("cur", j === cursor));
    counter.textContent = `step ${cursor + 1} of ${events.length}`;
    prev.disabled = cursor === 0;
    next.disabled = cursor === events.length - 1;
    nodes[cursor].scrollIntoView({ block: "nearest" });
  };
  prev.addEventListener("click", () => show(cursor - 1));
  next.addEventListener("click", () => show(cursor + 1));

  const controls = el("div", "controls");
  controls.append(prev, next, counter);
  wrap.append(controls, tape);
  wrap.append(el("p", "aside",
    "What was recorded: the model's reasoning, every tool call with its arguments, " +
    "and whether each call errored. A result line is a one-line summary the harness " +
    "logged, not the command's output — the output itself was never written to disk. " +
    `Entries marked “cut at ${TRANSCRIPT_CAP} chars” were truncated by the recorder.`));
  show(0);
  return wrap;
}

function bodyText(ev) {
  if (ev.kind === "tool_call") return typeof ev.args === "string" ? ev.args : JSON.stringify(ev.args, null, 1);
  if (ev.kind === "localize") {
    const locs = (ev.locations || []).map(
      (l) => `  ${l.file}${l.class_name ? ` :: ${l.class_name}` : ""}${l.function_name ? ` :: ${l.function_name}` : ""}`
    );
    return [`files: ${(ev.files || []).join(", ")}`, ...locs].join("\n");
  }
  if (ev.kind === "sample") {
    return `sample ${ev.index}${ev.path ? ` → ${ev.path}` : ""}` +
      `${ev.usable === false ? " (rejected)" : ""}` + (ev.text ? `\n${ev.text}` : "");
  }
  return ev.text || "";
}

function renderDiff(diff) {
  const wrap = el("div", "diff");
  if (!diff.trim()) {
    wrap.append(el("p", "empty", "This arm produced no patch."));
    return wrap;
  }
  const det = el("details");
  const lines = diff.split("\n");
  det.append(el("summary", null, `Patch it submitted (${lines.length} lines)`));
  const pre = el("pre");
  for (const line of lines) {
    let cls = null;
    if (line.startsWith("+") && !line.startsWith("+++")) cls = "a";
    else if (line.startsWith("-") && !line.startsWith("---")) cls = "d";
    else if (line.startsWith("@@") || line.startsWith("diff --git") ||
             line.startsWith("index ") || line.startsWith("+++") || line.startsWith("---")) cls = "h";
    pre.append(el("span", cls, line + "\n"));
  }
  det.append(pre);
  wrap.append(det);
  return wrap;
}
