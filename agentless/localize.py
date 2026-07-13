"""
Agentless Phase 1 — Fault Localization.

Given a GitHub issue, identify which files and line ranges need to change.
Uses a repo map (file tree + function/class signatures) as context for the LLM.

Grounded in: "Agentless: Demystifying LLM-based Software Engineering Agents"
(Xia et al., 2024, UIUC). The key insight is that localization can be done
WITHOUT tool-use — just give the LLM a structured repo map and ask it to
identify suspect locations.

Three-level localization:
1. File-level: which files are most likely to contain the bug?
2. Class/function-level: which function/method needs changing?
3. Line-level: which lines within that function?
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from agent.llm import LLMConfig, complete
from observability.tracing import get_tracer
from sandbox.docker_workspace import DockerWorkspace

tracer = get_tracer(__name__)


@dataclass
class LocalizationResult:
    """Output of the localization phase."""

    suspect_files: list[str]  # Ranked list of file paths most likely to contain bug
    suspect_locations: list[dict]  # [{file, class, function, line_range, reason}]
    repo_map: str  # The repo map used (for debugging)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


def build_repo_map(workspace: DockerWorkspace, max_files: int = 100) -> str:
    """
    Build a compact repo map: file tree + function/class signatures.

    This is the context the LLM uses to localize the bug. We include:
    - The full file tree (filtered to .py files)
    - For the top-level source directory: all class and function names
    - File sizes (to help the LLM avoid reading huge files)

    Args:
        workspace:  Active DockerWorkspace.
        max_files:  Maximum number of files to include function signatures for.

    Returns:
        A compact string representation of the repo structure.
    """
    with tracer.start_as_current_span("agentless.build_repo_map"):
        # Get all Python files
        result = workspace.run(
            "find /repo -name '*.py' -not -path '*/.git/*' "
            "-not -path '*/node_modules/*' -not -path '*/__pycache__/*' "
            "| sort | head -300"
        )
        all_files = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]

        lines = ["=== REPOSITORY MAP ===\n"]
        lines.append("File tree (Python files only):")

        # Build tree
        for f in all_files:
            rel = f.replace("/repo/", "")
            lines.append(f"  {rel}")

        lines.append("\n=== CLASS AND FUNCTION SIGNATURES ===\n")

        # Extract signatures from source files (not test files, not __init__)
        source_files = [
            f
            for f in all_files[:max_files]
            if not any(skip in f for skip in ["/test_", "_test.py", "/__init__", "/setup.py"])
        ]

        for filepath in source_files[:50]:  # Cap at 50 files for context efficiency
            sigs_result = workspace.run(
                f"grep -n '^\\(class\\|    def\\|^def\\) ' '{filepath}' 2>/dev/null | head -30"
            )
            if sigs_result.stdout.strip():
                rel = filepath.replace("/repo/", "")
                lines.append(f"\n{rel}:")
                for sig_line in sigs_result.stdout.splitlines():
                    lines.append(f"  {sig_line}")

        return "\n".join(lines)


def localize(
    workspace: DockerWorkspace,
    issue_text: str,
    llm: LLMConfig,
) -> LocalizationResult:
    """
    Phase 1: Identify which files/functions need to change to fix the issue.

    Args:
        workspace:  Active DockerWorkspace with the repo.
        issue_text: The full issue title + body.
        llm:        Provider/model/api-key config (BYOK).

    Returns:
        LocalizationResult with ranked suspect files and locations.
    """
    with tracer.start_as_current_span("agentless.localize"):
        repo_map = build_repo_map(workspace)

        prompt = f"""You are a debugging expert. Given a GitHub issue and a repository map, identify exactly which files and functions need to be changed to fix the issue.

<repository_map>
{repo_map}
</repository_map>

<issue>
{issue_text}
</issue>

Analyze the issue carefully. Look for:
1. Class names, method names, or parameter names mentioned in the error
2. The file most likely to contain the implementation (not tests, not __init__)
3. The specific function or method that needs changing

Respond with a JSON object in this exact format:
{{
  "suspect_files": ["path/relative/to/repo.py", ...],
  "suspect_locations": [
    {{
      "file": "path/relative/to/repo.py",
      "class_name": "ClassName or null",
      "function_name": "function_or_method_name",
      "reason": "brief explanation of why this location is suspect"
    }}
  ]
}}

Return ONLY the JSON object, no other text."""

        resp = complete(llm, [{"role": "user", "content": prompt}], max_tokens=1024)

        in_tok = resp.input_tokens
        out_tok = resp.output_tokens
        cost = resp.cost_usd

        raw = resp.text.strip()

        # Parse JSON — strip markdown fences if present
        raw = re.sub(r"^```(?:json)?", "", raw, flags=re.MULTILINE).strip()
        raw = re.sub(r"```$", "", raw, flags=re.MULTILINE).strip()

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            # Fallback: extract JSON block
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            parsed = (
                json.loads(match.group(0))
                if match
                else {"suspect_files": [], "suspect_locations": []}
            )

        # Convert relative paths to absolute
        suspect_files = [
            f"/repo/{f}" if not f.startswith("/") else f for f in parsed.get("suspect_files", [])
        ]
        locations = parsed.get("suspect_locations", [])
        for loc in locations:
            if loc.get("file") and not loc["file"].startswith("/"):
                loc["file"] = f"/repo/{loc['file']}"

        return LocalizationResult(
            suspect_files=suspect_files,
            suspect_locations=locations,
            repo_map=repo_map,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=cost,
        )
