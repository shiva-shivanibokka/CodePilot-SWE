"""
Agentless pipeline — orchestrates the 3 phases end to end.

Usage:
    from agentless.pipeline import run_agentless
    from sandbox.docker_workspace import DockerWorkspace

    with DockerWorkspace.create(repo_url, commit_sha) as ws:
        result = run_agentless(ws, issue_text)
        print(f"Resolved: {result.resolved}, Cost: ${result.total_cost_usd:.4f}")
"""

from __future__ import annotations

import time

from agent.llm import LLMConfig
from agentless.localize import localize
from agentless.repair import repair
from agentless.validate import AgentlessResult, validate
from observability.metrics import metrics
from observability.tracing import get_tracer
from sandbox.docker_workspace import DockerWorkspace

tracer = get_tracer(__name__)


def run_agentless(
    workspace: DockerWorkspace,
    issue_text: str,
    llm: LLMConfig,
    num_samples: int = 10,
    test_command: str = "pytest tests/ -x -q --tb=short --timeout=60 2>&1",
    verbose: bool = False,
) -> AgentlessResult:
    """
    Run the full agentless pipeline on a single task.

    Phase 1: Localize — identify suspect files/functions
    Phase 2: Repair   — sample N candidate patches
    Phase 3: Validate — run tests on each candidate, select the best

    Args:
        workspace:    Active DockerWorkspace.
        issue_text:   Full GitHub issue text.
        llm:          Provider/model/api-key config (BYOK).
        num_samples:  Number of patch candidates to generate.
        test_command: Pytest command to validate patches.
        verbose:      If True, print phase progress.

    Returns:
        AgentlessResult with the best patch and benchmark data.
    """
    t0 = time.monotonic()
    metrics.task_started(approach="agentless")

    with tracer.start_as_current_span("agentless.pipeline") as span:
        span.set_attribute("task_id", workspace.task_id)

        # ── Phase 1: Localize ──────────────────────────────────────────────
        if verbose:
            print("[agentless] Phase 1: Localizing...")
        loc = localize(workspace, issue_text, llm)
        if verbose:
            print(
                f"[agentless] Localized to {len(loc.suspect_files)} files, "
                f"{len(loc.suspect_locations)} locations. "
                f"Cost: ${loc.cost_usd:.4f}"
            )

        # ── Phase 2: Repair ────────────────────────────────────────────────
        if verbose:
            print(f"[agentless] Phase 2: Generating {num_samples} patch candidates...")
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

        # ── Phase 3: Validate ──────────────────────────────────────────────
        if verbose:
            print("[agentless] Phase 3: Validating candidates...")
        result = validate(
            workspace,
            rep,
            localize_cost_usd=loc.cost_usd,
            localize_input_tokens=loc.input_tokens,
            localize_output_tokens=loc.output_tokens,
            test_command=test_command,
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

        return result
