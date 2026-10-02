"""
What of an agent's diff is allowed to reach the graded run.

Grading copies **only the agent's source changes** onto a pristine checkout
and then adds the held-out tests. Everything that can change how tests are
collected or judged — test files, `conftest.py`, pytest configuration,
interpreter start-up hooks — is dropped from the diff first, and the drop is
reported in the result.

Why this matters: pytest loads every `conftest.py` on the path to a test. An
agent that writes

    def pytest_runtest_makereport(item, call): ...   # rewrite every outcome to "passed"

next to the held-out tests turns any patch into a resolved one. Autonomous-
SWE-Agent applied the held-out test patch on top of the agent's working tree,
and CodePilot's eval wrote its held-out file into the agent's tree, so in both
an agent-written `conftest.py` was live during grading.
`tests/test_bench_grading.py` proves it no longer flips a result.

The rules, and their cost:

* Any path with a `test`, `tests` or `testing` directory component, any
  `test_*.py` / `*_test.py`, and every file the task's own test patch touches.
  A gold patch occasionally edits a helper under a tests directory; such a
  change is dropped here too, which can only make a correct patch fail, never
  make a wrong one pass.
* `conftest.py`, `pytest.ini`, `tox.ini`, `sitecustomize.py`,
  `usercustomize.py` and `*.pth` anywhere.
* `setup.cfg` and `pyproject.toml` only when the diff for that file mentions
  pytest (an `[tool.pytest.ini_options]` or `[tool:pytest]` section, or
  `addopts`), since both commonly carry legitimate packaging changes.
"""

from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import PurePosixPath

from codepilot.bench.checkout import split_diff

TEST_DIRS = {"test", "tests", "testing"}
ALWAYS_DROPPED = ("conftest.py", "pytest.ini", "tox.ini", "sitecustomize.py", "usercustomize.py")
CONFIG_WITH_PYTEST = ("setup.cfg", "pyproject.toml")
_PYTEST_MENTION = re.compile(r"^[+-].*(pytest|addopts)", re.MULTILINE | re.IGNORECASE)


def is_test_path(path: str) -> bool:
    p = PurePosixPath(path)
    if any(part in TEST_DIRS for part in p.parts[:-1]):
        return True
    return fnmatch(p.name, "test_*.py") or fnmatch(p.name, "*_test.py")


def drop_reason(path: str, file_diff: str, protected: set[str]) -> str | None:
    """Why a file's change must not reach grading, or None to keep it."""
    name = PurePosixPath(path).name
    if path in protected:
        return "touched by the held-out tests"
    if name in ALWAYS_DROPPED or name.endswith(".pth"):
        return "can change how tests are collected or judged"
    if name in CONFIG_WITH_PYTEST and _PYTEST_MENTION.search(file_diff):
        return "changes pytest configuration"
    if is_test_path(path):
        return "test file"
    return None


def filter_source_diff(diff: str, protected: set[str] | None = None) -> tuple[str, dict[str, str]]:
    """Split a diff into the part grading may apply and what was dropped.

    Returns (kept_diff, {path: reason}).
    """
    protected = protected or set()
    kept: list[str] = []
    dropped: dict[str, str] = {}
    for part in split_diff(diff):
        reason = drop_reason(part.path, part.text, protected)
        if reason:
            dropped[part.path] = reason
        else:
            kept.append(part.text)
    return "".join(kept), dropped
