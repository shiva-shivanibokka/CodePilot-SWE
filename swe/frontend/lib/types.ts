export type ProviderKey = "anthropic" | "openai" | "google" | "groq";

export interface Model {
  id: string;
  label: string;
}

export interface Provider {
  key: string;
  label: string;
  keyUrl: string;
  models: Model[];
}

export interface ProvidersResponse {
  providers: Provider[];
  live_runs_enabled: boolean;
}

export type Approach = "agent" | "agentless";

/** A streamed agent event — mirrors agent.loop.AgentEvent on the backend. */
export interface AgentEvent {
  type:
    | "thought"
    | "tool_call"
    | "tool_result"
    | "cost_update"
    | "context_compressed"
    | "done"
    | "error"
    | "heartbeat";
  data?: Record<string, unknown>;
  turn?: number;
  /** replay only: ms to wait before showing this event (not sent by the backend) */
  delayMs?: number;
}

export interface BenchmarkSummary {
  approach: string;
  model: string;
  resolve_rate: number;
  resolved_count: number;
  total_instances: number;
  avg_cost_usd: number;
  total_cost_usd: number;
  avg_turns: number;
}
