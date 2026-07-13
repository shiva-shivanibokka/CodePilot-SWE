"""
Core agent loop.

This is the heart of the agentic approach. A single agent runs in a loop,
calling tools and observing results until it decides the task is complete
or we hit a hard limit.

Design (directly mirrors Anthropic's SWE-bench setup):
- Raw Anthropic SDK — no LangGraph, no LangChain
- One model call per turn, with full message history
- Tool calls are dispatched synchronously inside the loop
- The loop stops when the model outputs <DONE> or hits max_turns / context limit
- Every turn is traced with OpenTelemetry
- Cost is tracked per-call (input + output tokens × price)

Streaming events:
    The loop yields AgentEvent objects as it runs. These are consumed by:
    - The WebSocket handler (for the live UI)
    - The eval harness (for logging)
    - Tests (for assertions)
"""

from __future__ import annotations

import os
import time
from collections.abc import Generator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agent.context import compress_messages, get_budget_status, should_compress
from agent.llm import LLMConfig, LLMError, assistant_message, complete
from agent.prompts import DONE_MARKER, SYSTEM_PROMPT, build_user_message
from agent.tools import TOOL_SCHEMAS, run_bash, run_editor, run_search
from agent.tools.search import clear_index
from observability.metrics import metrics
from observability.tracing import get_tracer
from sandbox.docker_workspace import DockerWorkspace

tracer = get_tracer(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────

MAX_TURNS = int(os.getenv("AGENT_MAX_TURNS", "50"))


# ── Event types ────────────────────────────────────────────────────────────────


class EventType(str, Enum):
    THOUGHT = "thought"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    COST_UPDATE = "cost_update"
    CONTEXT_COMPRESSED = "context_compressed"
    DONE = "done"
    ERROR = "error"


@dataclass
class AgentEvent:
    """A structured event emitted by the agent loop, for streaming to the UI."""

    type: EventType
    data: dict[str, Any] = field(default_factory=dict)
    turn: int = 0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "type": self.type.value,
            "data": self.data,
            "turn": self.turn,
            "timestamp": self.timestamp,
        }


# ── Result dataclass ───────────────────────────────────────────────────────────


@dataclass
class TaskResult:
    """Final result of an agent task run."""

    resolved: bool
    conclusion: str  # The text inside <DONE>...</DONE>
    diff: str  # git diff of all changes made
    turns: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    duration_seconds: float
    events: list[AgentEvent]
    stop_reason: str  # "done" | "max_turns" | "context_limit" | "error"
    error: str | None = None


# ── Main agent loop ────────────────────────────────────────────────────────────


def run_agent(
    workspace: DockerWorkspace,
    issue_text: str,
    llm: LLMConfig,
    max_turns: int = MAX_TURNS,
) -> Generator[AgentEvent, None, TaskResult]:
    """
    Run the agent loop on a single task.

    This is a generator function. It yields AgentEvent objects as the agent
    works, and returns a TaskResult when done.

    Args:
        workspace:  Active DockerWorkspace with the repo checked out.
        issue_text: The full GitHub issue text.
        llm:        Provider/model/api-key config (BYOK) — the caller's own key.
        max_turns:  Hard limit on number of agent turns.

    Yields:
        AgentEvent objects (thought, tool_call, tool_result, cost_update, done)

    Returns:
        TaskResult (via StopIteration.value in Python generators)
    """
    model = llm.model
    task_id = workspace.task_id

    # Message history — accumulates over the session
    messages: list[dict[str, Any]] = []

    # Add the first user message
    messages.append(
        {
            "role": "user",
            "content": build_user_message(issue_text),
        }
    )

    t0 = time.monotonic()
    total_input_tokens = 0
    total_output_tokens = 0
    total_cost_usd = 0.0
    events: list[AgentEvent] = []
    turn = 0
    stop_reason = "error"
    conclusion = ""
    error_message: str | None = None

    metrics.task_started(approach="agent")

    def emit(event: AgentEvent) -> AgentEvent:
        events.append(event)
        return event

    with tracer.start_as_current_span("agent.task") as task_span:
        task_span.set_attribute("task_id", task_id)
        task_span.set_attribute("model", model)

        try:
            while turn < max_turns:
                turn += 1

                # ── Check context budget ──────────────────────────────────────
                if should_compress(messages):
                    messages = compress_messages(messages)
                    event = emit(
                        AgentEvent(
                            type=EventType.CONTEXT_COMPRESSED,
                            data=get_budget_status(messages),
                            turn=turn,
                        )
                    )
                    yield event

                budget = get_budget_status(messages)
                if budget["percent_used"] >= 99:
                    stop_reason = "context_limit"
                    break

                # ── Call the model ────────────────────────────────────────────
                with tracer.start_as_current_span("agent.llm_call") as llm_span:
                    llm_span.set_attribute("turn", turn)

                    resp = complete(
                        llm,
                        messages,
                        system=SYSTEM_PROMPT,
                        tools=TOOL_SCHEMAS,
                        max_tokens=4096,
                    )

                    in_tok = resp.input_tokens
                    out_tok = resp.output_tokens
                    total_input_tokens += in_tok
                    total_output_tokens += out_tok
                    total_cost_usd += resp.cost_usd

                    llm_span.set_attribute("input_tokens", in_tok)
                    llm_span.set_attribute("output_tokens", out_tok)
                    llm_span.set_attribute("cost_usd", resp.cost_usd)

                metrics.tokens_used(in_tok, out_tok, approach="agent")

                cost_event = emit(
                    AgentEvent(
                        type=EventType.COST_UPDATE,
                        data={
                            "turn_input_tokens": in_tok,
                            "turn_output_tokens": out_tok,
                            "turn_cost_usd": round(resp.cost_usd, 5),
                            "total_cost_usd": round(total_cost_usd, 5),
                            "total_input_tokens": total_input_tokens,
                            "total_output_tokens": total_output_tokens,
                        },
                        turn=turn,
                    )
                )
                yield cost_event

                # ── Process assistant text + tool calls ───────────────────────
                text = resp.text.strip()
                if text:
                    yield emit(AgentEvent(type=EventType.THOUGHT, data={"text": text}, turn=turn))
                    if DONE_MARKER in text:
                        start = text.find(DONE_MARKER) + len(DONE_MARKER)
                        end = text.find("</DONE>", start)
                        conclusion = (
                            text[start:end].strip() if end > start else text[start:].strip()
                        )

                for tc in resp.tool_calls:
                    yield emit(
                        AgentEvent(
                            type=EventType.TOOL_CALL,
                            data={"tool_name": tc.name, "tool_id": tc.id, "input": tc.arguments},
                            turn=turn,
                        )
                    )

                # ── Append assistant message (carries tool_calls) ─────────────
                messages.append(assistant_message(resp))

                # ── Check stop conditions ─────────────────────────────────────
                if not resp.tool_calls:
                    # No tools requested: the model is finished (<DONE> or just stopped)
                    stop_reason = "done" if conclusion else "no_action"
                    break

                if conclusion:
                    stop_reason = "done"
                    break

                # ── Execute tool calls; each needs a matching tool result ─────
                for tc in resp.tool_calls:
                    tool_result_str = _dispatch_tool(workspace, tc.name, tc.arguments, task_id)

                    yield emit(
                        AgentEvent(
                            type=EventType.TOOL_RESULT,
                            data={
                                "tool_name": tc.name,
                                "tool_id": tc.id,
                                "result": tool_result_str[:2000],  # truncate for event display
                            },
                            turn=turn,
                        )
                    )

                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": tool_result_str}
                    )

        except LLMError as exc:
            error_message = str(exc)
            yield emit(
                AgentEvent(type=EventType.ERROR, data={"error": error_message}, turn=turn)
            )
            stop_reason = "error"

        finally:
            # Always clean up the search index
            clear_index(task_id)

        # ── Collect final diff ────────────────────────────────────────────────
        diff = workspace.get_diff()
        duration = time.monotonic() - t0

        # ── Determine resolution ──────────────────────────────────────────────
        # A task is "resolved" if:
        # 1. The agent output <DONE>
        # 2. AND there is a non-empty diff (code was actually changed)
        # Note: true resolution requires running the SWE-bench test harness,
        # which happens in eval/harness.py. This is a heuristic for the live UI.
        resolved_heuristic = bool(conclusion) and bool(diff.strip())

        done_event = emit(
            AgentEvent(
                type=EventType.DONE,
                data={
                    "stop_reason": stop_reason,
                    "conclusion": conclusion,
                    "turns": turn,
                    "total_cost_usd": round(total_cost_usd, 5),
                    "total_input_tokens": total_input_tokens,
                    "total_output_tokens": total_output_tokens,
                    "duration_seconds": round(duration, 1),
                    "diff_lines": len(diff.splitlines()),
                },
                turn=turn,
            )
        )
        yield done_event

        # Count an errored run as an error, not a (failed) completion, so the
        # resolve-rate gauge isn't diluted by tasks that never really ran.
        if stop_reason == "error":
            metrics.task_errored(approach="agent")
        else:
            metrics.task_completed(
                resolved=resolved_heuristic,
                approach="agent",
                cost_usd=total_cost_usd,
                turns=turn,
                duration_seconds=duration,
            )

        return TaskResult(
            resolved=resolved_heuristic,
            conclusion=conclusion,
            diff=diff,
            turns=turn,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            cost_usd=total_cost_usd,
            duration_seconds=duration,
            events=events,
            stop_reason=stop_reason,
            error=error_message,
        )


def _dispatch_tool(
    workspace: DockerWorkspace,
    tool_name: str,
    tool_input: dict[str, Any],
    task_id: str,
) -> str:
    """Route a tool call to the correct handler."""
    if tool_name == "bash":
        return run_bash(workspace, tool_input.get("command", ""))
    elif tool_name == "str_replace_editor":
        return run_editor(workspace, tool_input)
    elif tool_name == "search_codebase":
        return run_search(
            workspace,
            query=tool_input.get("query", ""),
            file_pattern=tool_input.get("file_pattern"),
            top_k=tool_input.get("top_k", 10),
            task_id=task_id,
        )
    else:
        return f"Error: Unknown tool '{tool_name}'. Available tools: bash, str_replace_editor, search_codebase"
