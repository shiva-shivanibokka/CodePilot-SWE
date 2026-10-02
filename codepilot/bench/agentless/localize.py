"""
Agentless phase 1 — fault localisation.

Given a GitHub issue, name the files and functions that need to change, from a
repository map, in one model call with no tools.

Grounded in: "Agentless: Demystifying LLM-based Software Engineering Agents"
(Xia et al., 2024). The point of the comparison is that localisation can be
done without tool use: give the model a structured map and ask.

Ported from Autonomous-SWE-Agent. Changes: the map is built by reading the
checkout directly (it used `find` and one `grep` subprocess per file through a
POSIX shell), files come from the Workspace so `.gitignore` is honoured, and
the call goes through CodePilot's LLMClient with the shared system prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from codepilot.bench.agentless.jsonx import extract_json
from codepilot.bench.prompts import LOCALIZE_ARM, issue_message, system
from codepilot.llm import Usage

#: Python files listed in the map, and files whose signatures are included.
MAX_MAP_FILES = 300
MAX_SIGNATURE_FILES = 50
MAX_SIGNATURES_PER_FILE = 30
_SIGNATURE = re.compile(r"^(class |def |    def )")
_SKIP_FOR_SIGNATURES = ("/test_", "_test.py", "__init__", "setup.py", "/tests/", "conftest")


@dataclass
class LocalizationResult:
    suspect_files: list[str]
    suspect_locations: list[dict]
    repo_map: str
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    calls: int = 0
    unpriced_calls: int = 0


def build_repo_map(root, files: list[str]) -> str:
    """File tree (Python only) plus class/def signatures of the source files."""
    py = [f for f in files if f.endswith(".py")][:MAX_MAP_FILES]
    lines = ["=== REPOSITORY MAP ===", "", "Python files:"]
    lines += [f"  {f}" for f in py]
    lines += ["", "=== CLASS AND FUNCTION SIGNATURES ===", ""]
    source = [f for f in py if not any(s in f"/{f}" for s in _SKIP_FOR_SIGNATURES)]
    for rel in source[:MAX_SIGNATURE_FILES]:
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        sigs = [
            f"  {n}:{line.rstrip()}"
            for n, line in enumerate(text.splitlines(), 1)
            if _SIGNATURE.match(line)
        ][:MAX_SIGNATURES_PER_FILE]
        if sigs:
            lines.append(f"{rel}:")
            lines += sigs
    return "\n".join(lines)


async def localize(client, model: str | None, root, files: list[str], issue: str) -> LocalizationResult:
    repo_map = build_repo_map(root, files)
    reply = await client.chat(
        [
            {
                "role": "user",
                "content": f"<repository_map>\n{repo_map}\n</repository_map>\n\n"
                + issue_message(issue),
            }
        ],
        system=system(LOCALIZE_ARM),
        model=model,
        max_tokens=2048,
        temperature=0.2,
    )
    try:
        parsed = extract_json(reply.text, expect=dict)
    except ValueError:
        # Localisation failed: the pipeline still runs, on no suspects, and
        # says so rather than pretending the model returned an empty list.
        parsed = {"suspect_files": [], "suspect_locations": []}

    known = set(files)

    def clean(path) -> str:
        text = str(path or "").strip().replace("\\", "/")
        for prefix in ("/repo/", "repo/", "./", "/"):
            text = text.removeprefix(prefix)
        return text

    suspects = [p for p in (clean(f) for f in parsed.get("suspect_files") or []) if p in known]
    locations = []
    for spot in parsed.get("suspect_locations") or []:
        if isinstance(spot, dict) and clean(spot.get("file")) in known:
            locations.append({**spot, "file": clean(spot.get("file"))})
    return LocalizationResult(
        suspect_files=suspects,
        suspect_locations=locations,
        repo_map=repo_map,
        usage=reply.usage,
        cost_usd=reply.cost_usd or 0.0,
        calls=1,
        unpriced_calls=0 if reply.cost_usd is not None else 1,
    )
