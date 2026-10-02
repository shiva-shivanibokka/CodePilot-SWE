import type { AgentEvent, Approach, BenchmarkSummary, ProvidersResponse } from "./types";

// Where the API lives. Default: the Vercel app's own /api routes (providers +
// benchmark, hosted for free). Point NEXT_PUBLIC_API_BASE at the local Python
// backend (e.g. http://localhost:8000) to enable live agent runs.
export const API_BASE = (process.env.NEXT_PUBLIC_API_BASE || "/api").replace(/\/$/, "");

/** The provider/model registry + whether this instance permits live runs. */
export async function fetchProviders(): Promise<ProvidersResponse | null> {
  try {
    const res = await fetch(`${API_BASE}/providers`, { cache: "no-store" });
    if (!res.ok) return null;
    return (await res.json()) as ProvidersResponse;
  } catch {
    return null;
  }
}

/** Latest benchmark summaries, if the backend serves them. Never fabricated. */
export async function fetchBenchmark(): Promise<BenchmarkSummary[] | null> {
  try {
    const res = await fetch(`${API_BASE}/benchmark`, { cache: "no-store" });
    if (!res.ok) return null;
    const body = await res.json();
    return (body.summaries ?? null) as BenchmarkSummary[] | null;
  } catch {
    return null;
  }
}

export interface StartTaskArgs {
  issueUrl: string;
  approach: Approach;
  provider: string;
  model: string;
  apiKey: string;
}

export interface StartTaskResult {
  taskId: string;
  websocketUrl: string;
}

/** Kick off a live BYOK run. Throws with the backend's message on failure. */
export async function startTask(args: StartTaskArgs): Promise<StartTaskResult> {
  const res = await fetch(`${API_BASE}/tasks`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      issue_url: args.issueUrl,
      approach: args.approach,
      provider: args.provider,
      model: args.model,
      api_key: args.apiKey,
      create_pr: false,
    }),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || `Request failed (${res.status})`);
  }
  const body = await res.json();
  return { taskId: body.task_id, websocketUrl: body.websocket_url };
}

/** Stream events from a live run. Returns a cleanup function. */
export function streamEvents(
  websocketUrl: string,
  onEvent: (e: AgentEvent) => void,
  onClose: () => void,
): () => void {
  const ws = new WebSocket(websocketUrl);
  ws.onmessage = (msg) => {
    try {
      const e = JSON.parse(msg.data) as AgentEvent;
      if (e.type !== "heartbeat") onEvent(e);
    } catch {
      /* ignore malformed frames */
    }
  };
  ws.onclose = onClose;
  ws.onerror = onClose;
  return () => ws.close();
}
