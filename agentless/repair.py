"""
Agentless Phase 2 — Patch Generation.

Given the localized files/functions, sample N candidate patches from the LLM.
Each patch is a unified diff applied to the suspect file.

Key difference from the agentic approach:
- The LLM does NOT use tools. It reads the file content we provide in the prompt.
- We sample N=10 patches (with temperature > 0) and pick the best one in Phase 3.
- Each patch is self-contained: the LLM outputs the exact replacement code.
- This is cheaper than running a full agent loop for each candidate.

Grounded in: Agentless paper — they sample 10 patches and use test execution
to select the best one, achieving 32% on SWE-bench Lite at $0.70/issue.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from agent.llm import LLMConfig, complete
from agentless.localize import LocalizationResult
from observability.tracing import get_tracer
from sandbox.docker_workspace import DockerWorkspace

tracer = get_tracer(__name__)

NUM_SAMPLES = int(os.getenv("AGENTLESS_NUM_SAMPLES", "10"))


@dataclass
class PatchCandidate:
    """A single candidate patch."""

    file_path: str
    original_content: str
    patched_content: str
    explanation: str
    sample_index: int
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def unified_diff(self) -> str:
        """Return a unified diff of the patch."""
        import difflib

        diff = list(
            difflib.unified_diff(
                self.original_content.splitlines(keepends=True),
                self.patched_content.splitlines(keepends=True),
                fromfile=f"a/{self.file_path}",
                tofile=f"b/{self.file_path}",
                lineterm="",
            )
        )
        return "".join(diff)


@dataclass
class RepairResult:
    """Output of the repair phase."""

    candidates: list[PatchCandidate]
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0


def repair(
    workspace: DockerWorkspace,
    issue_text: str,
    localization: LocalizationResult,
    llm: LLMConfig,
    num_samples: int = NUM_SAMPLES,
) -> RepairResult:
    """
    Phase 2: Generate N candidate patches for the localized locations.

    For each suspect location:
    1. Read the actual file content from the workspace
    2. Ask the LLM to produce a fixed version (N times, with temperature=1.0)
    3. Return all candidates for validation in Phase 3

    Args:
        workspace:      Active DockerWorkspace.
        issue_text:     Full issue text.
        localization:   Output from Phase 1 (localize()).
        llm:            Provider/model/api-key config (BYOK).
        num_samples:    Number of patch candidates to generate.

    Returns:
        RepairResult with all candidates.
    """
    with tracer.start_as_current_span("agentless.repair"):
        candidates = []
        total_in_tok = 0
        total_out_tok = 0
        total_cost = 0.0

        # Focus on the top suspect location(s)
        locations = localization.suspect_locations[:3]  # Top 3 locations max
        if not locations:
            # Fall back to top suspect files
            locations = [
                {"file": f, "function_name": None, "class_name": None, "reason": ""}
                for f in localization.suspect_files[:2]
            ]

        for loc in locations:
            filepath = loc.get("file", "")
            if not filepath:
                continue

            try:
                file_content = workspace.read_file(filepath)
            except FileNotFoundError:
                continue

            # Build repair prompt
            function_hint = ""
            if loc.get("function_name"):
                cls = loc.get("class_name", "")
                fn = loc["function_name"]
                function_hint = (
                    f"\nFocus on the `{cls}.{fn}` method."
                    if cls
                    else f"\nFocus on the `{fn}` function."
                )

            prompt = f"""You are fixing a bug in a Python file. Here is the issue:

<issue>
{issue_text}
</issue>

Here is the file that needs to be fixed:

<file path="{filepath}">
{file_content}
</file>
{function_hint}

Produce the COMPLETE fixed file content. Make the MINIMAL change needed.
Do not add unnecessary changes, comments, or reformatting.

Respond with a JSON object:
{{
  "explanation": "one sentence explaining what you changed and why",
  "fixed_content": "complete fixed file content as a string"
}}

Return ONLY the JSON object."""

            # Sample num_samples patches per location
            samples_per_location = max(1, num_samples // len(locations))

            for sample_idx in range(samples_per_location):
                resp = complete(
                    llm,
                    [{"role": "user", "content": prompt}],
                    temperature=1.0 if sample_idx > 0 else 0.2,
                    max_tokens=4096,
                )

                in_tok = resp.input_tokens
                out_tok = resp.output_tokens
                cost = resp.cost_usd
                total_in_tok += in_tok
                total_out_tok += out_tok
                total_cost += cost

                raw = resp.text.strip()
                raw = re.sub(r"^```(?:json)?", "", raw, flags=re.MULTILINE).strip()
                raw = re.sub(r"```$", "", raw, flags=re.MULTILINE).strip()

                try:
                    parsed = json.loads(raw)
                    fixed_content = parsed.get("fixed_content", "")
                    explanation = parsed.get("explanation", "")

                    if fixed_content and fixed_content != file_content:
                        candidates.append(
                            PatchCandidate(
                                file_path=filepath,
                                original_content=file_content,
                                patched_content=fixed_content,
                                explanation=explanation,
                                sample_index=len(candidates),
                                input_tokens=in_tok,
                                output_tokens=out_tok,
                                cost_usd=cost,
                            )
                        )
                except (json.JSONDecodeError, KeyError):
                    continue

        return RepairResult(
            candidates=candidates,
            total_input_tokens=total_in_tok,
            total_output_tokens=total_out_tok,
            total_cost_usd=total_cost,
        )
