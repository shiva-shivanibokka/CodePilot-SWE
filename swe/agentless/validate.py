"""
Agentless Phase 3 — Patch Validation.

For each candidate patch:
1. Write the patched file to the sandbox
2. Run the test suite
3. Count how many tests pass
4. Rank candidates by pass rate
5. Select the best patch (or None if all fail)

This is the key advantage of the agentless approach: we don't need the LLM to
be perfect on the first try. We sample 10 candidates and run tests to pick the
best one. This is much cheaper than running a full agent loop.

The test execution is sandboxed (Docker) so bad patches can't affect the host.
"""

from __future__ import annotations

import posixpath
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from agent.loop import AgentEvent, EventType
from agentless.repair import PatchCandidate, RepairResult
from observability.tracing import get_tracer
from sandbox.workspace import Workspace

tracer = get_tracer(__name__)


@dataclass
class ValidationResult:
    """Result of running tests against a single patch candidate."""

    candidate: PatchCandidate
    tests_passed: int
    tests_failed: int
    tests_error: int
    test_output: str
    duration_seconds: float
    valid: bool  # True if all relevant tests pass
    command: str = ""  # what was actually run, once {scope} was resolved


@dataclass
class AgentlessResult:
    """Final result of the agentless pipeline."""

    best_candidate: PatchCandidate | None
    best_validation: ValidationResult | None
    all_validations: list[ValidationResult]
    resolved: bool  # True if best candidate passes all tests
    total_cost_usd: float
    total_input_tokens: int
    total_output_tokens: int


def _count(word: str, output: str) -> int:
    """Total for one pytest outcome word, summed over every summary line present."""
    return sum(int(n) for n in re.findall(rf"(\d+) {word}s?\b", output))


def _parse_pytest_output(output: str) -> tuple[int, int, int]:
    """
    Extract pass/fail/error counts from pytest's summary line.

    Each outcome is matched independently. Pytest orders its summary worst-first
    ("2 failed, 5 passed"), so a single regex expecting "passed" ahead of
    "failed" matches the passes, never sees the failures, and reports a run with
    failing tests as clean — which here promotes a broken patch to the winner.
    """
    return _count("passed", output), _count("failed", output), _count("error", output)


def tests_near(workspace: Workspace, file_path: str) -> str:
    """
    The test directory closest to a patched file, walking upwards.

    Running a repository's entire suite per candidate is what made this phase
    unrunnable on the benchmark's real repositories: sympy's full suite takes
    the better part of an hour, and ten candidates means ten of those. The
    tests that sit beside the file being changed are the ones that would catch
    a regression in it, and they run in seconds.
    """
    # `file_exists` answers for files only on both backends, so a directory is
    # probed by listing it: empty means it is not there, or has nothing to run.
    directory = posixpath.dirname(file_path.replace("/repo/", ""))
    while directory:
        candidate_dir = posixpath.join(directory, "tests")
        if workspace.list_files(candidate_dir):
            return candidate_dir
        directory = posixpath.dirname(directory)
    return "tests" if workspace.list_files("tests") else "."


def resolve_command(workspace: Workspace, template: str, file_path: str) -> str:
    """Fill `{scope}` in a test command with the tests nearest the patched file."""
    if "{scope}" not in template:
        return template
    return template.replace("{scope}", tests_near(workspace, file_path))


def measure(workspace: Workspace, command: str) -> tuple[int, int, int]:
    """
    Run the tests as the repository currently stands.

    A candidate cannot be judged against zero failures, because a checkout of a
    years-old commit rarely has zero: pinned dependencies have moved on, and
    tests unrelated to the issue are already red. Judging against an absolute
    `failed == 0` throws away every candidate on such a repository - including
    correct ones - and reports it as the model failing to write a patch.
    """
    result = workspace.run(command, timeout=600)
    return _parse_pytest_output(result.output)


def validate_candidate(
    workspace: Workspace,
    candidate: PatchCandidate,
    test_command: str = "pytest tests/ -x -q --tb=short --timeout=60 2>&1",
    baseline: tuple[int, int, int] | None = None,
) -> ValidationResult:
    """
    Apply a patch to the workspace and run the test suite.

    Args:
        workspace:      Workspace with the original repo.
        candidate:      The patch to test.
        test_command:   The pytest command to run.

    Returns:
        ValidationResult with test counts and output.
    """
    with tracer.start_as_current_span("agentless.validate_candidate"):
        t0 = time.monotonic()

        # Write the patched file
        workspace.write_file(candidate.file_path, candidate.patched_content)

        # `{scope}` lets the caller ask for tests near the patched file rather
        # than naming a directory it cannot know ahead of time.
        command = resolve_command(workspace, test_command, candidate.file_path)

        # Run the tests
        result = workspace.run(command, timeout=600)
        output = result.output

        passed, failed, errors = _parse_pytest_output(output)
        duration = time.monotonic() - t0

        # Restore original file regardless of result
        workspace.write_file(candidate.file_path, candidate.original_content)

        # A patch is kept when it breaks nothing that worked and fixes at
        # least as much as before. Without a baseline this asked for a clean
        # suite, which on these repositories means asking for something that
        # was not true before the model touched anything.
        base_passed, base_failed, base_errors = baseline or (0, 0, 0)
        valid = (
            passed > 0
            and failed <= base_failed
            and errors <= base_errors
            and passed >= base_passed
        )

        return ValidationResult(
            candidate=candidate,
            tests_passed=passed,
            tests_failed=failed,
            tests_error=errors,
            test_output=output[:3000],  # truncate for storage
            duration_seconds=round(duration, 1),
            valid=valid,
            command=command,
        )


def validate(
    workspace: Workspace,
    repair_result: RepairResult,
    localize_cost_usd: float = 0.0,
    localize_input_tokens: int = 0,
    localize_output_tokens: int = 0,
    test_command: str = "pytest tests/ -x -q --tb=short --timeout=60 2>&1",
    on_event: Callable[[AgentEvent], None] | None = None,
) -> AgentlessResult:
    """
    Phase 3: Validate all candidates and select the best patch.

    Args:
        workspace:              Workspace.
        repair_result:          Output from repair().
        localize_cost_usd:      Cost from localization phase (for totals).
        localize_input_tokens:  Tokens from localization phase.
        localize_output_tokens: Tokens from localization phase.
        test_command:           Pytest command to run. May contain `{scope}`,
                                replaced per candidate with the test directory
                                nearest the file that candidate patches.
        on_event:               Called once per candidate with its verdict.

    Returns:
        AgentlessResult with the best candidate and validation results.
    """
    with tracer.start_as_current_span("agentless.validate"):
        validations: list[ValidationResult] = []
        # One baseline per distinct command, measured before anything is
        # patched. Candidates that patch the same area share it.
        baselines: dict[str, tuple[int, int, int]] = {}

        for candidate in repair_result.candidates:
            command = resolve_command(workspace, test_command, candidate.file_path)
            if command not in baselines:
                baselines[command] = measure(workspace, command)
                if on_event:
                    base = baselines[command]
                    on_event(
                        AgentEvent(
                            type=EventType.TOOL_RESULT,
                            data={
                                "tool_name": "baseline",
                                "result": (
                                    f"before any patch: {base[0]} passed, {base[1]} failed, "
                                    f"{base[2]} errors - this is what a candidate has to beat"
                                ),
                            },
                        )
                    )

            val = validate_candidate(workspace, candidate, test_command, baselines[command])
            validations.append(val)
            if on_event:
                on_event(
                    AgentEvent(
                        type=EventType.TOOL_RESULT,
                        data={
                            "tool_name": "validate",
                            "result": (
                                f"#{candidate.sample_index} {candidate.file_path}: "
                                f"{val.tests_passed} passed, {val.tests_failed} failed, "
                                f"{val.tests_error} errors in {val.duration_seconds}s "
                                f"({'kept' if val.valid else 'rejected'})"
                            ),
                        },
                    )
                )

            # Early exit if we find a perfect patch
            if val.valid:
                break

        # Rank: prefer valid patches, then by tests_passed desc, then tests_failed asc
        validations.sort(
            key=lambda v: (
                -int(v.valid),
                -v.tests_passed,
                v.tests_failed + v.tests_error,
            )
        )

        best = validations[0] if validations else None
        resolved = best.valid if best else False

        if resolved and best:
            # Apply the winning patch permanently
            workspace.write_file(best.candidate.file_path, best.candidate.patched_content)

        total_cost = localize_cost_usd + repair_result.total_cost_usd
        total_in = localize_input_tokens + repair_result.total_input_tokens
        total_out = localize_output_tokens + repair_result.total_output_tokens

        return AgentlessResult(
            best_candidate=best.candidate if best else None,
            best_validation=best,
            all_validations=validations,
            resolved=resolved,
            total_cost_usd=total_cost,
            total_input_tokens=total_in,
            total_output_tokens=total_out,
        )
