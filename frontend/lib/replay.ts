import type { AgentEvent } from "./types";

/**
 * A bundled SAMPLE agent run used when no live backend is available (the hosted
 * demo is replay-only, since live runs need a Docker sandbox). It is a
 * representative trace of the agentic loop resolving a real scikit-learn issue —
 * illustrative, not a benchmarked result. Replace with a real recording via
 * `python -m eval.run_eval --record` (see backend README).
 */
export const SAMPLE_RUN = {
  issueUrl: "https://github.com/scikit-learn/scikit-learn/issues/12462",
  title: "RidgeClassifierCV missing store_cv_values",
  approach: "agent" as const,
  events: [
    { type: "thought", turn: 1, delayMs: 300, data: { text: "Reading the issue: RidgeClassifierCV should accept store_cv_values like RidgeCV does, but the parameter is missing. I'll locate the class first." } },
    { type: "tool_call", turn: 1, delayMs: 700, data: { tool_name: "search_codebase", input: { query: "RidgeClassifierCV store_cv_values" } } },
    { type: "tool_result", turn: 1, delayMs: 900, data: { tool_name: "search_codebase", result: "[1] sklearn/linear_model/ridge.py:1340  class RidgeClassifierCV(...)\n[2] sklearn/linear_model/ridge.py:1180  class _BaseRidgeCV(...)" } },
    { type: "cost_update", turn: 1, delayMs: 250, data: { total_cost_usd: 0.014, total_input_tokens: 4120, total_output_tokens: 260 } },
    { type: "thought", turn: 2, delayMs: 500, data: { text: "RidgeClassifierCV extends _BaseRidgeCV. RidgeCV exposes store_cv_values; the classifier just needs to pass it through __init__. Let me view the constructor." } },
    { type: "tool_call", turn: 2, delayMs: 650, data: { tool_name: "str_replace_editor", input: { command: "view", path: "/repo/sklearn/linear_model/ridge.py", view_range: [1340, 1375] } } },
    { type: "tool_result", turn: 2, delayMs: 800, data: { tool_name: "str_replace_editor", result: "1340  class RidgeClassifierCV(LinearClassifierMixin, _BaseRidgeCV):\n1361      def __init__(self, alphas=(0.1, 1.0, 10.0), fit_intercept=True,\n1362                   normalize=False, scoring=None, cv=None, class_weight=None):" } },
    { type: "cost_update", turn: 2, delayMs: 250, data: { total_cost_usd: 0.031, total_input_tokens: 8730, total_output_tokens: 540 } },
    { type: "thought", turn: 3, delayMs: 500, data: { text: "Adding store_cv_values=False to the signature and forwarding it to super().__init__, mirroring RidgeCV." } },
    { type: "tool_call", turn: 3, delayMs: 700, data: { tool_name: "str_replace_editor", input: { command: "str_replace", path: "/repo/sklearn/linear_model/ridge.py" } } },
    { type: "tool_result", turn: 3, delayMs: 700, data: { tool_name: "str_replace_editor", result: "Replaced 1 occurrence. store_cv_values now forwarded to _BaseRidgeCV.__init__." } },
    { type: "thought", turn: 4, delayMs: 500, data: { text: "Writing a quick reproduction to confirm the attribute now exists after fit()." } },
    { type: "tool_call", turn: 4, delayMs: 650, data: { tool_name: "bash", input: { command: "python -c \"from sklearn.linear_model import RidgeClassifierCV; import numpy as np; m=RidgeClassifierCV(store_cv_values=True).fit(np.random.randn(20,5), np.random.randint(0,2,20)); print(hasattr(m,'cv_values_'))\"" } } },
    { type: "tool_result", turn: 4, delayMs: 1100, data: { tool_name: "bash", result: "True" } },
    { type: "thought", turn: 5, delayMs: 500, data: { text: "Reproduction passes. Running the relevant tests to confirm nothing regressed." } },
    { type: "tool_call", turn: 5, delayMs: 650, data: { tool_name: "bash", input: { command: "pytest sklearn/linear_model/tests/test_ridge.py -q -k store_cv_values" } } },
    { type: "tool_result", turn: 5, delayMs: 1300, data: { tool_name: "bash", result: "4 passed in 3.21s" } },
    { type: "cost_update", turn: 5, delayMs: 250, data: { total_cost_usd: 0.058, total_input_tokens: 16240, total_output_tokens: 910 } },
    { type: "thought", turn: 6, delayMs: 500, data: { text: "Tests pass. <DONE>Added store_cv_values to RidgeClassifierCV.__init__ and forwarded it to _BaseRidgeCV, matching RidgeCV.</DONE>" } },
    { type: "done", turn: 6, delayMs: 400, data: { stop_reason: "done", resolved: true, turns: 6, total_cost_usd: 0.058, diff_lines: 6 } },
  ] as AgentEvent[],
};

/** Play a recorded run, invoking onEvent with the original inter-event timing. */
export async function playRun(
  events: AgentEvent[],
  onEvent: (e: AgentEvent) => void,
  shouldStop: () => boolean,
): Promise<void> {
  for (const e of events) {
    if (shouldStop()) return;
    await new Promise((r) => setTimeout(r, e.delayMs ?? 500));
    if (shouldStop()) return;
    onEvent(e);
  }
}
