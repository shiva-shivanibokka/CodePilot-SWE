"""
SWE-bench-lite evaluation harness.

Runs both the agentic and agentless approaches on SWE-bench-lite instances,
records results, and produces the comparison table for the README.

SWE-bench-lite is 300 real GitHub issues from popular Python repos.
Each instance has:
  - instance_id: unique identifier (e.g. "scikit-learn__scikit-learn-12462")
  - repo:        GitHub repo slug (e.g. "scikit-learn/scikit-learn")
  - base_commit: the commit just BEFORE the issue was fixed
  - problem_statement: the issue text
  - hints_text:  optional hints (we don't use these for fair eval)
  - test_patch:  the test diff used to grade the solution (we don't show this to the agent)
  - PASS_TO_PASS / FAIL_TO_PASS: the specific test IDs that must pass

Grading:
  An instance is "resolved" if and only if ALL of its FAIL_TO_PASS tests
  now pass (they failed on the base commit), AND ALL PASS_TO_PASS tests
  still pass (they passed on the base commit and must not be broken).
  This is the official SWE-bench grading criteria.

Usage:
    python -m eval.run_eval --approach agent --limit 10
    python -m eval.run_eval --approach agentless --limit 10
    python -m eval.run_eval --compare  # runs both, produces comparison table
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from agent.llm import LLMConfig
from observability.tracing import get_tracer

tracer = get_tracer(__name__)

RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)

EVAL_MAX_WORKERS = int(os.getenv("EVAL_MAX_WORKERS", "4"))
EVAL_INSTANCE_LIMIT = int(os.getenv("EVAL_INSTANCE_LIMIT", "300"))
GRADE_TIMEOUT = int(os.getenv("EVAL_GRADE_TIMEOUT", "600"))
REPO_ROOT = "/repo"
REGRESSION_TEST_COMMAND = "python -m pytest -x -q --tb=short 2>&1"
# Capped so one instance with 300 PASS_TO_PASS ids cannot dominate a run.
MAX_GRADED_TESTS = int(os.getenv("EVAL_MAX_GRADED_TESTS", "20"))


@dataclass
class InstanceResult:
    """Result of running an approach on a single SWE-bench instance."""

    instance_id: str
    repo: str
    approach: str  # "agent" | "agentless"
    resolved: bool
    cost_usd: float
    turns: int  # 0 for agentless
    input_tokens: int
    output_tokens: int
    duration_seconds: float
    stop_reason: str
    diff: str  # the patch that was submitted
    error: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass
class EvalRun:
    """Aggregated results for a full evaluation run."""

    approach: str
    model: str
    total_instances: int
    resolved_count: int
    failed_count: int
    error_count: int
    resolve_rate: float
    avg_cost_usd: float
    total_cost_usd: float
    avg_turns: float
    avg_duration_seconds: float
    avg_input_tokens: float
    avg_output_tokens: float
    run_id: str
    timestamp: str
    instance_results: list[InstanceResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["instance_results"] = [asdict(r) for r in self.instance_results]
        return d


HF_ROWS_URL = (
    "https://datasets-server.huggingface.co/rows"
    "?dataset=princeton-nlp%2FSWE-bench_Lite&config=default&split=test"
    "&offset={offset}&length={length}"
)
SWEBENCH_LITE_SIZE = 300


def _load_via_http(limit: int | None = None) -> list[dict]:
    """
    Read SWE-bench-lite straight from the dataset's public rows API.

    The `swebench` package pulls the whole `datasets` stack in order to hand
    back 300 dictionaries. This is the same data over plain HTTP, so listing or
    recording a single instance does not require a heavyweight install.
    """
    import json as _json
    import urllib.request

    wanted = min(limit or SWEBENCH_LITE_SIZE, SWEBENCH_LITE_SIZE)
    rows: list[dict] = []
    for offset in range(0, wanted, 100):
        url = HF_ROWS_URL.format(offset=offset, length=min(100, wanted - offset))
        with urllib.request.urlopen(url, timeout=60) as response:
            rows += [row["row"] for row in _json.load(response)["rows"]]
    return rows


def load_swebench_lite(limit: int | None = None) -> list[dict]:
    """
    Load SWE-bench-lite instances.

    Prefers the `swebench` package when it is installed (pip install -e ".[eval]")
    and falls back to the dataset's HTTP API, which needs no extra dependency.

    Returns instance dicts with keys:
        instance_id, repo, base_commit, problem_statement,
        hints_text, test_patch, FAIL_TO_PASS, PASS_TO_PASS, ...
    """
    try:
        from swebench.harness.utils import load_swebench_dataset

        instances = list(load_swebench_dataset("princeton-nlp/SWE-bench_Lite", split="test"))
    except ImportError:
        instances = _load_via_http(limit)

    return instances[:limit] if limit else instances


def load_instance(instance_id: str) -> dict:
    """Load one SWE-bench-lite instance by id, e.g. 'pallets__flask-4992'."""
    for instance in load_swebench_lite():
        if instance["instance_id"] == instance_id:
            return instance
    raise KeyError(f"No SWE-bench-lite instance named {instance_id!r}")


def build_test_command(instance: dict) -> str:
    """
    Build the pytest command that grades a SWE-bench instance.

    Runs exactly the FAIL_TO_PASS and PASS_TO_PASS ids the instance names, so
    a patch is judged on the tests the real fix was judged on. No --timeout
    flag: that needs pytest-timeout, which most target repos do not install,
    and pytest then exits 4 on an unrecognised argument — indistinguishable
    from a failing test. Hangs are caught by the workspace's own deadline.
    """
    fail_to_pass = instance.get("FAIL_TO_PASS", "[]")
    pass_to_pass = instance.get("PASS_TO_PASS", "[]")

    if isinstance(fail_to_pass, str):
        try:
            fail_to_pass = json.loads(fail_to_pass)
        except json.JSONDecodeError:
            fail_to_pass = []
    if isinstance(pass_to_pass, str):
        try:
            pass_to_pass = json.loads(pass_to_pass)
        except json.JSONDecodeError:
            pass_to_pass = []

    all_tests = list(fail_to_pass) + list(pass_to_pass)

    if all_tests:
        test_spec = " ".join(f'"{t}"' for t in all_tests[:MAX_GRADED_TESTS])
        return f"python -m pytest {test_spec} -x -q --tb=short 2>&1"
    return "python -m pytest -x -q --tb=short 2>&1"


def apply_test_patch(workspace, instance: dict) -> bool:
    """
    Apply the instance's test patch to the checkout.

    SWE-bench grades against tests written as part of the real fix, which do not
    exist at the base commit. Without applying this patch first, every
    FAIL_TO_PASS id names a test that isn't there, pytest exits non-zero on a
    collection error, and both approaches score 0% no matter how good the patch
    was. It is applied only after the agent has finished, so the agent never
    sees the tests it is judged on.
    """
    test_patch = instance.get("test_patch") or ""
    if not test_patch.strip():
        return False

    workspace.write_file(f"{REPO_ROOT}/.swebench_test_patch.diff", test_patch)
    result = workspace.run(f"git apply -v {REPO_ROOT}/.swebench_test_patch.diff")
    if not result.success:
        # Fall back to patch(1), which tolerates fuzz that git apply rejects.
        result = workspace.run(f"patch -p1 -i {REPO_ROOT}/.swebench_test_patch.diff")
    return result.success


def grade(workspace, instance: dict, diff: str) -> bool:
    """
    Decide whether an instance was resolved.

    This is the in-harness proxy, not the official grader: it applies the test
    patch and runs the FAIL_TO_PASS / PASS_TO_PASS ids the instance names,
    capped at 20 tests. The official swebench grader re-runs the full sets in
    their own images; treat these numbers as indicative and say so wherever
    they are published.
    """
    if not diff or not diff.strip():
        return False
    if not apply_test_patch(workspace, instance):
        return False
    return workspace.run(build_test_command(instance), timeout=GRADE_TIMEOUT).success


def run_instance_agent(
    instance: dict,
    llm: LLMConfig,
) -> InstanceResult:
    """Run the agentic approach on a single SWE-bench instance."""
    from agent.loop import run_agent
    from sandbox import create_workspace

    instance_id = instance["instance_id"]
    repo = instance["repo"]
    base_commit = instance["base_commit"]
    issue_text = instance.get("problem_statement", "")

    repo_url = f"https://github.com/{repo}.git"

    with tracer.start_as_current_span("eval.agent_instance") as span:
        span.set_attribute("instance_id", instance_id)

        try:
            with create_workspace(
                repo_url=repo_url,
                commit_sha=base_commit,
                task_id=instance_id[:8],
            ) as workspace:
                # Consume the generator to completion
                gen = run_agent(workspace, issue_text, llm)
                task_result = None
                try:
                    while True:
                        next(gen)
                except StopIteration as e:
                    task_result = e.value

                diff = workspace.get_diff()
                resolved = grade(workspace, instance, diff)

                return InstanceResult(
                    instance_id=instance_id,
                    repo=repo,
                    approach="agent",
                    resolved=resolved,
                    cost_usd=task_result.cost_usd if task_result else 0.0,
                    turns=task_result.turns if task_result else 0,
                    input_tokens=task_result.input_tokens if task_result else 0,
                    output_tokens=task_result.output_tokens if task_result else 0,
                    duration_seconds=task_result.duration_seconds if task_result else 0.0,
                    stop_reason=task_result.stop_reason if task_result else "error",
                    diff=diff,
                )

        except Exception as exc:
            return InstanceResult(
                instance_id=instance_id,
                repo=repo,
                approach="agent",
                resolved=False,
                cost_usd=0.0,
                turns=0,
                input_tokens=0,
                output_tokens=0,
                duration_seconds=0.0,
                stop_reason="error",
                diff="",
                error=str(exc),
            )


def run_instance_agentless(
    instance: dict,
    llm: LLMConfig,
) -> InstanceResult:
    """Run the agentless approach on a single SWE-bench instance."""
    from agentless.pipeline import run_agentless
    from sandbox import create_workspace

    instance_id = instance["instance_id"]
    repo = instance["repo"]
    base_commit = instance["base_commit"]
    issue_text = instance.get("problem_statement", "")
    repo_url = f"https://github.com/{repo}.git"

    with tracer.start_as_current_span("eval.agentless_instance") as span:
        span.set_attribute("instance_id", instance_id)

        try:
            with create_workspace(
                repo_url=repo_url,
                commit_sha=base_commit,
                task_id=instance_id[:8],
            ) as workspace:
                # Candidate selection runs the repo's *existing* suite as a
                # regression check. Handing it the graded tests would be marking
                # its own homework — those only appear at grading time, below.
                result = run_agentless(
                    workspace,
                    issue_text,
                    llm,
                    test_command=REGRESSION_TEST_COMMAND,
                )
                diff = workspace.get_diff()
                resolved = grade(workspace, instance, diff)

                return InstanceResult(
                    instance_id=instance_id,
                    repo=repo,
                    approach="agentless",
                    resolved=resolved,
                    cost_usd=result.total_cost_usd,
                    turns=0,
                    input_tokens=result.total_input_tokens,
                    output_tokens=result.total_output_tokens,
                    duration_seconds=0.0,
                    stop_reason="done" if resolved else "failed",
                    diff=diff,
                )

        except Exception as exc:
            return InstanceResult(
                instance_id=instance_id,
                repo=repo,
                approach="agentless",
                resolved=False,
                cost_usd=0.0,
                turns=0,
                input_tokens=0,
                output_tokens=0,
                duration_seconds=0.0,
                stop_reason="error",
                diff="",
                error=str(exc),
            )


def run_evaluation(
    approach: Literal["agent", "agentless"],
    instances: list[dict],
    llm: LLMConfig,
    max_workers: int = EVAL_MAX_WORKERS,
    progress_callback: Callable[[InstanceResult, int, int], None] | None = None,
) -> EvalRun:
    """
    Run the evaluation on a list of SWE-bench instances.

    Args:
        approach:          "agent" or "agentless"
        instances:         List of SWE-bench instance dicts
        llm:               Provider/model/api-key config (BYOK)
        max_workers:       Parallel workers (1 = sequential)
        progress_callback: Called after each instance with (result, done, total)

    Returns:
        EvalRun with aggregated statistics and all instance results
    """
    run_fn = run_instance_agent if approach == "agent" else run_instance_agentless
    run_id = f"{approach}_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"
    results: list[InstanceResult] = []
    total = len(instances)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(run_fn, inst, llm): inst for inst in instances}

        for i, future in enumerate(as_completed(futures), 1):
            result = future.result()
            results.append(result)

            # Save incremental results
            _save_result(result, run_id)

            if progress_callback:
                progress_callback(result, i, total)
            else:
                status = "RESOLVED" if result.resolved else ("ERROR" if result.error else "FAILED")
                print(
                    f"[{i}/{total}] {result.instance_id}: {status} "
                    f"(${result.cost_usd:.3f}, {result.duration_seconds:.0f}s)"
                )

    resolved = [r for r in results if r.resolved]
    errors = [r for r in results if r.error]
    failed = [r for r in results if not r.resolved and not r.error]

    eval_run = EvalRun(
        approach=approach,
        model=f"{llm.provider}/{llm.model}",
        total_instances=total,
        resolved_count=len(resolved),
        failed_count=len(failed),
        error_count=len(errors),
        resolve_rate=round(len(resolved) / total * 100, 2) if total else 0,
        avg_cost_usd=round(sum(r.cost_usd for r in results) / total, 4) if total else 0,
        total_cost_usd=round(sum(r.cost_usd for r in results), 4),
        avg_turns=round(sum(r.turns for r in results) / total, 1) if total else 0,
        avg_duration_seconds=round(sum(r.duration_seconds for r in results) / total, 1)
        if total
        else 0,
        avg_input_tokens=round(sum(r.input_tokens for r in results) / total, 0) if total else 0,
        avg_output_tokens=round(sum(r.output_tokens for r in results) / total, 0) if total else 0,
        run_id=run_id,
        timestamp=datetime.now(UTC).isoformat(),
        instance_results=results,
    )

    # Save final aggregated run
    run_path = RESULTS_DIR / f"{run_id}_summary.json"
    run_path.write_text(json.dumps(eval_run.to_dict(), indent=2))
    print(f"\n[eval] Results saved to {run_path}")

    return eval_run


def _save_result(result: InstanceResult, run_id: str) -> None:
    """Save an individual instance result to disk incrementally."""
    path = RESULTS_DIR / f"{run_id}_instances.jsonl"
    with open(path, "a") as f:
        f.write(json.dumps(asdict(result)) + "\n")


def print_comparison_table(agent_run: EvalRun, agentless_run: EvalRun) -> None:
    """Print the comparison table for the README."""
    print("\n" + "=" * 70)
    print("BENCHMARK COMPARISON — SWE-bench-lite")
    print("=" * 70)
    print(f"{'Metric':<30} {'Agent':>12} {'Agentless':>12}")
    print("-" * 56)
    print(
        f"{'% Resolved':<30} {agent_run.resolve_rate:>11.1f}% {agentless_run.resolve_rate:>11.1f}%"
    )
    print(
        f"{'Resolved / Total':<30} {agent_run.resolved_count:>7}/{agent_run.total_instances:<4} {agentless_run.resolved_count:>7}/{agentless_run.total_instances:<4}"
    )
    print(
        f"{'Avg Cost / Issue':<30} ${agent_run.avg_cost_usd:>10.3f} ${agentless_run.avg_cost_usd:>10.3f}"
    )
    print(
        f"{'Total Cost':<30} ${agent_run.total_cost_usd:>10.2f} ${agentless_run.total_cost_usd:>10.2f}"
    )
    print(f"{'Avg Turns / Issue':<30} {agent_run.avg_turns:>11.1f} {'—':>12}")
    print(
        f"{'Avg Duration (s)':<30} {agent_run.avg_duration_seconds:>11.0f} {agentless_run.avg_duration_seconds:>11.0f}"
    )
    print(
        f"{'Avg Input Tokens':<30} {agent_run.avg_input_tokens:>11,.0f} {agentless_run.avg_input_tokens:>11,.0f}"
    )
    print(f"{'Model':<30} {agent_run.model[:12]:>12} {agentless_run.model[:12]:>12}")
    print("=" * 70)
    print(f"\nRun IDs: agent={agent_run.run_id}, agentless={agentless_run.run_id}")
    print("Full results: eval/results/")
