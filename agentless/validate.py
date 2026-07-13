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

import re
import time
from dataclasses import dataclass

from agentless.repair import PatchCandidate, RepairResult
from observability.tracing import get_tracer
from sandbox.docker_workspace import DockerWorkspace

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


def _parse_pytest_output(output: str) -> tuple[int, int, int]:
    """Parse pytest output to extract pass/fail/error counts."""
    # Look for the summary line: "5 passed, 2 failed, 1 error"
    match = re.search(
        r"(\d+) passed(?:,\s*(\d+) failed)?(?:,\s*(\d+) error)?",
        output,
    )
    if match:
        passed = int(match.group(1) or 0)
        failed = int(match.group(2) or 0)
        errors = int(match.group(3) or 0)
        return passed, failed, errors

    # Alternative: just failed
    fail_match = re.search(r"(\d+) failed", output)
    pass_match = re.search(r"(\d+) passed", output)
    return (
        int(pass_match.group(1)) if pass_match else 0,
        int(fail_match.group(1)) if fail_match else 0,
        0,
    )


def validate_candidate(
    workspace: DockerWorkspace,
    candidate: PatchCandidate,
    test_command: str = "pytest tests/ -x -q --tb=short --timeout=60 2>&1",
) -> ValidationResult:
    """
    Apply a patch to the workspace and run the test suite.

    Args:
        workspace:      DockerWorkspace with the original repo.
        candidate:      The patch to test.
        test_command:   The pytest command to run.

    Returns:
        ValidationResult with test counts and output.
    """
    with tracer.start_as_current_span("agentless.validate_candidate"):
        t0 = time.monotonic()

        # Write the patched file
        workspace.write_file(candidate.file_path, candidate.patched_content)

        # Run the tests
        result = workspace.run(test_command, timeout=120)
        output = result.output

        passed, failed, errors = _parse_pytest_output(output)
        duration = time.monotonic() - t0

        # Restore original file regardless of result
        workspace.write_file(candidate.file_path, candidate.original_content)

        valid = failed == 0 and errors == 0 and passed > 0

        return ValidationResult(
            candidate=candidate,
            tests_passed=passed,
            tests_failed=failed,
            tests_error=errors,
            test_output=output[:3000],  # truncate for storage
            duration_seconds=round(duration, 1),
            valid=valid,
        )


def validate(
    workspace: DockerWorkspace,
    repair_result: RepairResult,
    localize_cost_usd: float = 0.0,
    localize_input_tokens: int = 0,
    localize_output_tokens: int = 0,
    test_command: str = "pytest tests/ -x -q --tb=short --timeout=60 2>&1",
) -> AgentlessResult:
    """
    Phase 3: Validate all candidates and select the best patch.

    Args:
        workspace:              DockerWorkspace.
        repair_result:          Output from repair().
        localize_cost_usd:      Cost from localization phase (for totals).
        localize_input_tokens:  Tokens from localization phase.
        localize_output_tokens: Tokens from localization phase.
        test_command:           Pytest command to run.

    Returns:
        AgentlessResult with the best candidate and validation results.
    """
    with tracer.start_as_current_span("agentless.validate"):
        validations: list[ValidationResult] = []

        for candidate in repair_result.candidates:
            val = validate_candidate(workspace, candidate, test_command)
            validations.append(val)

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
