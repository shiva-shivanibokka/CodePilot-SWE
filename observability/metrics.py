"""
Prometheus metrics for the SWE agent.

Exposes an HTTP /metrics endpoint that Prometheus scrapes. Tracks:
- Total tasks run and their outcomes (resolved / failed / error)
- Per-task token and cost accounting
- Tool call latency histograms
- Agent turn counts

Usage:
    from observability.metrics import metrics, start_metrics_server

    start_metrics_server(port=9090)

    # In your agent loop:
    metrics.task_started()
    metrics.task_completed(resolved=True, cost_usd=0.42, turns=18)
    metrics.tool_called("bash", duration_ms=340)
    metrics.tokens_used(input_tokens=1200, output_tokens=340)
"""

from __future__ import annotations

import os
import threading

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    start_http_server,
)


class AgentMetrics:
    """
    Central metrics registry for the SWE agent.

    All counters and histograms are pre-created at import time so Prometheus
    sees them immediately, even before any tasks run.
    """

    def __init__(self) -> None:
        # ── Task-level ─────────────────────────────────────────────────────
        self.tasks_total = Counter(
            "swe_agent_tasks_total",
            "Total number of tasks attempted",
            ["approach"],  # "agent" | "agentless"
        )
        self.tasks_resolved = Counter(
            "swe_agent_tasks_resolved_total",
            "Tasks where tests passed (resolved)",
            ["approach"],
        )
        self.tasks_failed = Counter(
            "swe_agent_tasks_failed_total",
            "Tasks that ran but did not resolve",
            ["approach"],
        )
        self.tasks_error = Counter(
            "swe_agent_tasks_error_total",
            "Tasks that errored out (exception, timeout, etc.)",
            ["approach"],
        )

        # ── Cost and tokens ────────────────────────────────────────────────
        self.cost_usd_total = Counter(
            "swe_agent_cost_usd_total",
            "Total LLM cost in USD",
            ["approach"],
        )
        self.input_tokens_total = Counter(
            "swe_agent_input_tokens_total",
            "Total input tokens consumed",
            ["approach"],
        )
        self.output_tokens_total = Counter(
            "swe_agent_output_tokens_total",
            "Total output tokens generated",
            ["approach"],
        )

        # ── Per-task distributions ─────────────────────────────────────────
        self.turns_per_task = Histogram(
            "swe_agent_turns_per_task",
            "Number of agent turns per task",
            ["approach"],
            buckets=[1, 5, 10, 20, 30, 50, 75, 100],
        )
        self.task_duration_seconds = Histogram(
            "swe_agent_task_duration_seconds",
            "Wall-clock time per task in seconds",
            ["approach"],
            buckets=[30, 60, 120, 300, 600, 1200, 1800],
        )
        self.cost_per_task = Histogram(
            "swe_agent_cost_per_task_usd",
            "LLM cost per task in USD",
            ["approach"],
            buckets=[0.01, 0.05, 0.10, 0.25, 0.50, 1.0, 2.0, 5.0],
        )

        # ── Tool calls ─────────────────────────────────────────────────────
        self.tool_calls_total = Counter(
            "swe_agent_tool_calls_total",
            "Total tool invocations",
            ["tool_name"],  # "bash" | "str_replace_editor" | "search_codebase"
        )
        self.tool_errors_total = Counter(
            "swe_agent_tool_errors_total",
            "Tool invocations that returned a non-zero exit code or error",
            ["tool_name"],
        )
        self.tool_duration_ms = Histogram(
            "swe_agent_tool_duration_ms",
            "Tool execution latency in milliseconds",
            ["tool_name"],
            buckets=[10, 50, 100, 250, 500, 1000, 2000, 5000, 10000],
        )

        # ── Live state ─────────────────────────────────────────────────────
        self.active_tasks = Gauge(
            "swe_agent_active_tasks",
            "Number of tasks currently running",
            ["approach"],
        )
        self.resolve_rate = Gauge(
            "swe_agent_resolve_rate",
            "Rolling resolve rate (resolved / total), updated after each task",
            ["approach"],
        )

        # Internal counters for computing rolling resolve rate
        self._totals: dict[str, int] = {}
        self._resolved: dict[str, int] = {}
        self._lock = threading.Lock()

    # ── Public API ─────────────────────────────────────────────────────────

    def task_started(self, approach: str = "agent") -> None:
        self.tasks_total.labels(approach=approach).inc()
        self.active_tasks.labels(approach=approach).inc()

    def task_completed(
        self,
        resolved: bool,
        approach: str = "agent",
        cost_usd: float = 0.0,
        turns: int = 0,
        duration_seconds: float = 0.0,
    ) -> None:
        self.active_tasks.labels(approach=approach).dec()

        if resolved:
            self.tasks_resolved.labels(approach=approach).inc()
        else:
            self.tasks_failed.labels(approach=approach).inc()

        self.cost_usd_total.labels(approach=approach).inc(cost_usd)
        self.cost_per_task.labels(approach=approach).observe(cost_usd)
        self.turns_per_task.labels(approach=approach).observe(turns)
        self.task_duration_seconds.labels(approach=approach).observe(duration_seconds)

        with self._lock:
            self._totals[approach] = self._totals.get(approach, 0) + 1
            if resolved:
                self._resolved[approach] = self._resolved.get(approach, 0) + 1
            rate = self._resolved.get(approach, 0) / self._totals[approach]
            self.resolve_rate.labels(approach=approach).set(rate)

    def task_errored(self, approach: str = "agent") -> None:
        self.active_tasks.labels(approach=approach).dec()
        self.tasks_error.labels(approach=approach).inc()

    def tool_called(
        self,
        tool_name: str,
        duration_ms: int = 0,
        error: bool = False,
    ) -> None:
        self.tool_calls_total.labels(tool_name=tool_name).inc()
        self.tool_duration_ms.labels(tool_name=tool_name).observe(duration_ms)
        if error:
            self.tool_errors_total.labels(tool_name=tool_name).inc()

    def tokens_used(
        self,
        input_tokens: int,
        output_tokens: int,
        approach: str = "agent",
    ) -> None:
        self.input_tokens_total.labels(approach=approach).inc(input_tokens)
        self.output_tokens_total.labels(approach=approach).inc(output_tokens)


# Singleton — import and use directly
metrics = AgentMetrics()

_server_started = False


def start_metrics_server(port: int | None = None) -> None:
    """Start the Prometheus HTTP server on the configured port."""
    global _server_started
    if _server_started:
        return
    port = port or int(os.getenv("PROMETHEUS_PORT", "9090"))
    start_http_server(port)
    _server_started = True
    print(f"[metrics] Prometheus metrics available at http://localhost:{port}/metrics")
