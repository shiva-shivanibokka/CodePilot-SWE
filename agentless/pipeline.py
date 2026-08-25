"""
Agentless pipeline — orchestrates the 3 phases end to end.

Usage:
    from agentless.pipeline import run_agentless
    from sandbox import create_workspace

    with create_workspace(repo_url, commit_sha) as ws:
        result = run_agentless(ws, issue_text)
        print(f"Resolved: {result.resolved}, Cost: ${result.total_cost_usd:.4f}")
"""

from __future__ import annotations

import time
from collections.abc import Callable

from agent.llm import LLMConfig
from agent.loop import AgentEvent, EventType
from agentless.localize import LocalizationResult, localize
from agentless.repair import repair
from agentless.validate import AgentlessResult, validate
from observability.metrics import metrics
from observability.tracing import get_tracer
from sandbox.workspace import Workspace

tracer = get_tracer(__name__)


def run_agentless(
    workspace: Workspace,
    issue_text: str,
    llm: LLMConfig,
    num_samples: int = 10,
    test_command: str = "pytest tests/ -x -q --tb=short --timeout=60 2>&1",
    verbose: bool = False,
    on_event: Callable[[AgentEvent], None] | None = None,
) -> AgentlessResult:
    """
    Run the full agentless pipeline on a single task.

    Phase 1: Localize — identify suspect files/functions
    Phase 2: Repair   — sample N candidate patches
    Phase 3: Validate — run tests on each candidate, select the best

    Args:
        workspace:    Active Workspace.
        issue_text:   Full GitHub issue text.
        llm:          Provider/model/api-key config (BYOK).
        num_samples:  Number of patch candidates to generate.
        test_command: Pytest command to validate patches.
        verbose:      If True, print phase progress.
        on_event:     Called with an AgentEvent at each phase boundary and for
                      every candidate generated or validated. The pipeline has
                      no turns to stream, so this is what makes a run observable
                      from outside - the recorder and the UI both consume the
                      same event shape the agent loop emits.

    Returns:
        AgentlessResult with the best patch and benchmark data.
    """
    t0 = time.monotonic()
    metrics.task_started(approach="agentless")

    def emit(kind: EventType, data: dict, turn: int = 0) -> None:
        if on_event:
            on_event(AgentEvent(type=kind, data=data, turn=turn))

    with tracer.start_as_current_span("agentless.pipeline") as span:
        span.set_attribute("task_id", workspace.task_id)

        # ── Phase 1: Localize ──────────────────────────────────────────────
        if verbose:
            print("[agentless] Phase 1: Localizing...")
        emit(
            EventType.THOUGHT,
            {
                "text": "Phase 1 - localize. No tools and no shell: a single pass "
                "over a map of the repository, naming the files the issue points at."
            },
            turn=1,
        )
        loc = localize(workspace, issue_text, llm)
        emit(
            EventType.TOOL_CALL,
            {
                "tool_name": "localize",
                "input": {"query": ", ".join(loc.suspect_files[:5]) or "nothing found"},
            },
            turn=1,
        )
        emit(
            EventType.TOOL_RESULT,
            {"tool_name": "localize", "result": _locations_summary(loc)},
            turn=1,
        )
        emit(
            EventType.COST_UPDATE,
            {
                "total_cost_usd": round(loc.cost_usd, 5),
                "total_input_tokens": loc.input_tokens,
                "total_output_tokens": loc.output_tokens,
            },
            turn=1,
        )
        if verbose:
            print(
                f"[agentless] Localized to {len(loc.suspect_files)} files, "
                f"{len(loc.suspect_locations)} locations. "
                f"Cost: ${loc.cost_usd:.4f}"
            )

        # ── Phase 2: Repair ────────────────────────────────────────────────
        if verbose:
            print(f"[agentless] Phase 2: Generating {num_samples} patch candidates...")
        emit(
            EventType.THOUGHT,
            {
                "text": f"Phase 2 - repair. Sample {num_samples} independent patches at "
                "temperature 1. None of them is chosen here; the tests choose."
            },
            turn=2,
        )
        rep = repair(
            workspace,
            issue_text,
            loc,
            llm,
            num_samples=num_samples,
        )
        if verbose:
            print(
                f"[agentless] Generated {len(rep.candidates)} valid candidates. "
                f"Cost: ${rep.total_cost_usd:.4f}"
            )
        if rep.rejected:
            emit(
                EventType.TOOL_RESULT,
                {
                    "tool_name": "repair",
                    "result": (
                        f"{len(rep.rejected)} sample(s) discarded before testing: "
                        + "; ".join(rep.rejected[:3])
                    ),
                },
                turn=2,
            )
        for cand in rep.candidates:
            emit(
                EventType.TOOL_CALL,
                {
                    "tool_name": "candidate",
                    "input": {"path": f"#{cand.sample_index} {cand.file_path}"},
                },
                turn=2,
            )
        emit(
            EventType.COST_UPDATE,
            {
                "total_cost_usd": round(loc.cost_usd + rep.total_cost_usd, 5),
                "total_input_tokens": loc.input_tokens + rep.total_input_tokens,
                "total_output_tokens": loc.output_tokens + rep.total_output_tokens,
            },
            turn=2,
        )

        # ── Phase 3: Validate ──────────────────────────────────────────────
        if verbose:
            print("[agentless] Phase 3: Validating candidates...")
        emit(
            EventType.THOUGHT,
            {
                "text": "Phase 3 - validate. Run each candidate against the tests the "
                "repository already had and keep the one that survives. The graded "
                "tests are not in the checkout yet, so nothing here marks its own homework."
            },
            turn=3,
        )
        result = validate(
            workspace,
            rep,
            localize_cost_usd=loc.cost_usd,
            localize_input_tokens=loc.input_tokens,
            localize_output_tokens=loc.output_tokens,
            test_command=test_command,
            on_event=lambda e: emit(e.type, e.data, turn=3),
        )

        duration = time.monotonic() - t0

        if verbose:
            status = "RESOLVED" if result.resolved else "FAILED"
            print(
                f"[agentless] {status} in {duration:.1f}s. Total cost: ${result.total_cost_usd:.4f}"
            )

        span.set_attribute("resolved", result.resolved)
        span.set_attribute("total_cost_usd", result.total_cost_usd)
        span.set_attribute("duration_seconds", duration)

        metrics.task_completed(
            resolved=result.resolved,
            approach="agentless",
            cost_usd=result.total_cost_usd,
            turns=0,
            duration_seconds=duration,
        )

        emit(
            EventType.DONE,
            {
                "stop_reason": "done" if result.best_candidate else "no_candidate",
                "conclusion": _conclusion(result),
                # Three phases, not turns. Reported under the same key so one
                # number means the same thing on both sides of the comparison.
                "turns": 3,
                "total_cost_usd": round(result.total_cost_usd, 5),
                "total_input_tokens": result.total_input_tokens,
                "total_output_tokens": result.total_output_tokens,
                "duration_seconds": round(duration, 1),
                "candidates": len(rep.candidates),
                "validated": len(result.all_validations),
            },
            turn=3,
        )

        return result


def _locations_summary(loc: LocalizationResult) -> str:
    """One line per suspect location, in the order the model ranked them."""
    if not loc.suspect_locations:
        return "no locations returned; falling back to the top suspect files"
    lines = []
    for spot in loc.suspect_locations[:6]:
        where = spot.get("function_name") or spot.get("class_name") or "module level"
        lines.append(f"{spot.get('file', '?')} :: {where}")
    return "\n".join(lines)


def _conclusion(result: AgentlessResult) -> str:
    """What the pipeline settled on, in the terms a reader needs."""
    if not result.best_candidate:
        return "No candidate patch survived parsing, so there was nothing to submit."
    best = result.best_validation
    verdict = "passes the tests the repository already had" if result.resolved else "does not pass cleanly"
    counts = f"{best.tests_passed} passed, {best.tests_failed} failed" if best else "not run"
    return (
        f"Selected candidate #{result.best_candidate.sample_index} in "
        f"{result.best_candidate.file_path}: it {verdict} ({counts})."
    )
