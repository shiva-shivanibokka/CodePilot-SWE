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

import os
from dataclasses import dataclass, field

from agent.llm import LLMConfig, complete, extract_json
from agentless.localize import LocalizationResult
from observability.tracing import get_tracer
from sandbox.workspace import Workspace

tracer = get_tracer(__name__)

NUM_SAMPLES = int(os.getenv("AGENTLESS_NUM_SAMPLES", "10"))
# Room for one search/replace pair. Doubled once on a reply that runs out.
SAMPLE_MAX_TOKENS = int(os.getenv("AGENTLESS_SAMPLE_MAX_TOKENS", "4096"))


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
    # Why each discarded sample was discarded. A sample that cannot be parsed
    # into an edit is paid for and then thrown away, so dropping the reason on
    # the floor hides both a cost and a prompt problem — and this phase already
    # had one bug of exactly that shape.
    rejected: list[str] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0


def repair(
    workspace: Workspace,
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
        workspace:      Active Workspace.
        issue_text:     Full issue text.
        localization:   Output from Phase 1 (localize()).
        llm:            Provider/model/api-key config (BYOK).
        num_samples:    Number of patch candidates to generate.

    Returns:
        RepairResult with all candidates.
    """
    with tracer.start_as_current_span("agentless.repair"):
        candidates: list[PatchCandidate] = []
        rejected: list[str] = []
        retried = 0
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

        # Localization routinely names the same function twice - once per
        # reason it found it - and each duplicate would otherwise be sent the
        # same file, the same prompt and half the sample budget. The samples
        # are paid for either way; spending them on one place twice buys
        # nothing that spending them on one place once does not.
        locations = list(
            {
                (
                    spot.get("file", ""),
                    spot.get("class_name") or "",
                    spot.get("function_name") or "",
                ): spot
                for spot in locations
            }.values()
        )

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

Make the MINIMAL change needed. Do not reformat, do not add comments, do not
fix anything the issue did not ask for.

Respond with a JSON object:
{{
  "explanation": "one sentence on what you changed and why",
  "search": "the exact lines to replace, copied character for character from the file above, including indentation",
  "replace": "what those lines should become"
}}

"search" must appear EXACTLY ONCE in the file. If the lines you want are not
unique, add surrounding lines one at a time until they are — and stop there.

Keep "search" as short as it can be while staying unique. A handful of lines is
normal; a whole function is not. Copying a long block wastes the response on
lines you are not changing, and a reply that runs out of room mid-JSON is
discarded entirely.

Return ONLY the JSON object."""

            # Sample num_samples patches per location
            samples_per_location = max(1, num_samples // len(locations))

            for sample_idx in range(samples_per_location):
                temperature = 1.0 if sample_idx > 0 else 0.2
                resp = complete(
                    llm,
                    [{"role": "user", "content": prompt}],
                    temperature=temperature,
                    max_tokens=SAMPLE_MAX_TOKENS,
                )

                total_in_tok += resp.input_tokens
                total_out_tok += resp.output_tokens
                total_cost += resp.cost_usd

                # A reply cut off mid-JSON is unusable, and was paid for in
                # full. Asking again with room to finish costs one more call;
                # discarding it costs the call that was already made and buys
                # nothing. Instructing the model to be brief did not stop this
                # on long functions - it is the response that is long, not the
                # prompt that is unclear.
                if resp.finish_reason == "length":
                    resp = complete(
                        llm,
                        [{"role": "user", "content": prompt}],
                        temperature=temperature,
                        max_tokens=SAMPLE_MAX_TOKENS * 2,
                    )
                    total_in_tok += resp.input_tokens
                    total_out_tok += resp.output_tokens
                    total_cost += resp.cost_usd
                    retried += 1

                patched, problem = apply_search_replace(file_content, resp)
                if patched is None:
                    rejected.append(problem)
                    continue

                candidates.append(
                    PatchCandidate(
                        file_path=filepath,
                        original_content=file_content,
                        patched_content=patched,
                        explanation=str(problem or ""),
                        sample_index=len(candidates),
                        input_tokens=resp.input_tokens,
                        output_tokens=resp.output_tokens,
                        cost_usd=resp.cost_usd,
                    )
                )

        if retried:
            print(f"[agentless] {retried} sample(s) re-asked after running out of room")

        if rejected:
            # Reported whether some survived or none did: a run that quietly
            # discards half its samples looks identical to one that discards
            # none, and they are not the same run.
            scope = "every sample" if not candidates else f"{len(rejected)} of the samples"
            print(f"[agentless] {scope} rejected: {rejected[:5]}")

        return RepairResult(
            candidates=candidates,
            rejected=rejected,
            total_input_tokens=total_in_tok,
            total_output_tokens=total_out_tok,
            total_cost_usd=total_cost,
        )


def apply_search_replace(file_content: str, resp) -> tuple[str | None, str]:
    """
    Turn one sampled search/replace pair into patched file content.

    Returns (patched_content, explanation) on success and (None, reason) on
    failure. Every rejection carries a reason: a phase that yields no patches
    needs to be able to say whether the model was truncated, hallucinated the
    lines, or matched in more than one place.
    """
    if resp.finish_reason == "length":
        return None, "response hit the token cap before the JSON closed"

    try:
        parsed = extract_json(resp.text, expect=dict)
    except ValueError:
        return None, "no JSON object in the response"

    search = parsed.get("search") or ""
    replace = parsed.get("replace")
    explanation = str(parsed.get("explanation") or "")

    if not search or replace is None:
        return None, "missing 'search' or 'replace'"

    occurrences = file_content.count(search)
    if occurrences == 0:
        return None, "'search' does not appear in the file"
    if occurrences > 1:
        return None, f"'search' matches {occurrences} places, not one"

    patched = file_content.replace(search, replace, 1)
    if patched == file_content:
        return None, "the patch changes nothing"
    return patched, explanation
