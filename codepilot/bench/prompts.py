"""
The prompts every benchmark arm is given.

Fairness rule: **every arm's system prompt starts with the same `SHARED_BASE`**,
byte for byte, and each request carries exactly one cache breakpoint, on the
last (and only) system block. What differs between arms is written down here
and in the README and bench/STUDY_PLAN.md, not discovered later:

| | agent | agentless localize | agentless repair |
|---|---|---|---|
| system prompt | SHARED_BASE + AGENT_ARM | SHARED_BASE + LOCALIZE_ARM | SHARED_BASE + REPAIR_ARM |
| user message | the issue | the issue + a repository map | the issue + one file + a focus hint |
| tools | CodePilot's tool set | none | none |
| turns | until `finish` or a budget | one | one per sample |
| temperature | 0.2 for attempt 1, 1.0 after | 0.2 | 0.2 for sample 1, 1.0 after |

The agent's text is adapted from CodePilot's own loop prompt and from
Autonomous-SWE-Agent's SWE-bench prompt (reproduce first, then fix, then
verify). Agentless's is Autonomous-SWE-Agent's, with the shared base in front.
"""

from __future__ import annotations

SHARED_BASE = """\
You are resolving a real GitHub issue in a Python repository. The repository is
checked out at the commit just before the issue was fixed, with no history
beyond that commit and no network access.

Your change will be judged by tests you cannot see. Before they run, every
change you made to test files, conftest.py or pytest configuration is thrown
away, so only your change to the source code counts.

Rules:
- Make the smallest source change that resolves the issue. Do not refactor,
  reformat, or fix anything the issue did not ask for.
- Match the surrounding code's conventions.
- Never invent file contents: base every edit on code you have actually seen.
"""

AGENT_ARM = """\
## How to work (tool-using agent)

You have tools to list, search, read and edit files and to run commands, in the
repository root. Paths are relative to that root.

1. Find the code: `search` for exact strings, `search_code` for the issue's
   words, `find_symbol` for a definition and its callers. `read_file` before
   you edit; long files can be read by line range.
2. Reproduce the problem first when you can: a short script run with
   `run_command` (`python repro.py`). Confirm you see the failure.
3. Fix it with `edit_file` (exact-string replacement).
4. Verify: re-run the reproduction, then run the tests nearest the code you
   changed — a single file or directory, e.g.
   `run_tests` with `python -m pytest path/to/tests/test_x.py -q`. The whole
   suite may take far too long.
5. Call `finish` with a one-paragraph summary of the root cause and the fix.
   If you cannot fix it, call `finish` and say what you found.
"""

LOCALIZE_ARM = """\
## Your job: localise (no tools, one reply)

Given the issue and a map of the repository, name the files and functions that
must change. Reply with a JSON object only:
{"suspect_files": ["path/relative/to/repo.py", ...],
 "suspect_locations": [{"file": "path.py", "class_name": "Name or null",
                        "function_name": "name", "reason": "why"}]}
Rank most likely first. Prefer implementation files over tests and __init__.
"""

REPAIR_ARM = """\
## Your job: repair (no tools, one reply)

Given the issue and one file, produce one minimal edit as a JSON object only:
{"explanation": "one sentence on what you changed and why",
 "search": "the exact lines to replace, copied character for character from the file, including indentation",
 "replace": "what those lines should become"}

"search" must appear EXACTLY ONCE in the file. If the lines you want are not
unique, add surrounding lines one at a time until they are, and stop there.
Keep "search" short: a handful of lines is normal, a whole function is not. A
reply that runs out of room mid-JSON is discarded.
"""


def system(arm_text: str) -> list[dict]:
    """The one system block for a request, with the cache breakpoint on it."""
    return [
        {
            "type": "text",
            "text": SHARED_BASE + "\n" + arm_text,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def issue_message(problem_statement: str) -> str:
    return f"<issue>\n{problem_statement.strip()}\n</issue>"
