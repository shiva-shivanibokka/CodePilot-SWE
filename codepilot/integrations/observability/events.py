"""Feed CodePilot's event stream into the Prometheus metrics.

Autonomous-SWE-Agent called `metrics.*` from inside its own loop, its tools and
its agentless pipeline — all of which were removed in the merge. CodePilot
already emits one event stream from every arm (ADR-5 in docs/DESIGN.md), so the
metrics attach to that instead, as one more subscriber, and nothing in the
agent has to know they exist.

    from codepilot.integrations.observability.events import attach
    detach = attach(ctx.events, approach="agent")
"""

from __future__ import annotations

from collections.abc import Callable

from codepilot.events import Event, EventStream, EventType
from codepilot.integrations.observability.metrics import AgentMetrics
from codepilot.integrations.observability.metrics import metrics as default_metrics


def attach(stream: EventStream, approach: str = "agent",
           registry: AgentMetrics | None = None) -> Callable[[], None]:
    m = registry or default_metrics
    state = {"cost": 0.0, "turns": 0, "started": False}

    def on_event(event: Event) -> None:
        if event.type is EventType.TURN_START and not state["started"]:
            state["started"] = True
            m.task_started(approach=approach)
        elif event.type is EventType.TOOL_RESULT:
            m.tool_called(str(event.data.get("tool", "?")), error=bool(event.data.get("is_error")))
        elif event.type is EventType.COST:
            state["turns"] += 1
            state["cost"] += float(event.data.get("cost_usd") or 0.0)
            m.tokens_used(int(event.data.get("input_tokens") or 0),
                          int(event.data.get("output_tokens") or 0), approach=approach)
        elif event.type in (EventType.DONE, EventType.ERROR) and "stopped_by" in event.data:
            if not state["started"]:
                m.task_started(approach=approach)
            m.task_completed(resolved=event.type is EventType.DONE, approach=approach,
                             cost_usd=state["cost"], turns=state["turns"])
            state.update(cost=0.0, turns=0, started=False)

    return stream.subscribe(on_event)
