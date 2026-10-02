"""A tiny SWE-bench-shaped task, built locally: no network, no dataset.

`make_task(tmp)` creates an "upstream" git repository with two commits — the
base (a bug in `calc.add`) and the fix — and returns a SWE-bench-style
instance dict whose `repo_url` is the upstream's file:// URL, plus the gold
patch. Everything the grading tests need, and nothing they can reach by
accident.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

BASE_CALC = "def add(a, b):\n    return a - b\n\n\ndef double(x):\n    return x * 2\n"
FIXED_CALC = "def add(a, b):\n    return a + b\n\n\ndef double(x):\n    return x * 2\n"
BASE_TESTS = "from calc import double\n\n\ndef test_double():\n    assert double(2) == 4\n"
ADDED_TEST = (
    "\n\ndef test_add():\n    from calc import add\n\n    assert add(2, 3) == 5\n"
)
GOLD_PATCH = """\
diff --git a/calc.py b/calc.py
--- a/calc.py
+++ b/calc.py
@@ -1,5 +1,5 @@
 def add(a, b):
-    return a - b
+    return a + b


 def double(x):
"""
TEST_PATCH = """\
diff --git a/tests/test_calc.py b/tests/test_calc.py
--- a/tests/test_calc.py
+++ b/tests/test_calc.py
@@ -3,3 +3,9 @@ from calc import double

 def test_double():
     assert double(2) == 4
+
+
+def test_add():
+    from calc import add
+
+    assert add(2, 3) == 5
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout.strip()


def make_task(tmp: Path) -> tuple[dict, str]:
    """Returns (instance, fix_commit_sha)."""
    up = tmp / "upstream"
    (up / "tests").mkdir(parents=True)
    git(up, "init", "-q")
    (up / "calc.py").write_text(BASE_CALC, encoding="utf-8", newline="\n")
    (up / "tests" / "test_calc.py").write_text(BASE_TESTS, encoding="utf-8", newline="\n")
    (up / ".gitignore").write_text("build/\n", encoding="utf-8", newline="\n")
    git(up, "add", "-A")
    git(up, "commit", "-qm", "base")
    base = git(up, "rev-parse", "HEAD")
    (up / "calc.py").write_text(FIXED_CALC, encoding="utf-8", newline="\n")
    (up / "tests" / "test_calc.py").write_text(BASE_TESTS + ADDED_TEST, encoding="utf-8", newline="\n")
    git(up, "add", "-A")
    git(up, "commit", "-qm", "THE GOLD FIX")
    fix = git(up, "rev-parse", "HEAD")
    instance = {
        "instance_id": "local__calc-1",
        "repo": "local/calc",
        "repo_url": up.as_uri(),
        "base_commit": base,
        "problem_statement": "calc.add subtracts instead of adding.",
        "patch": GOLD_PATCH,
        "test_patch": TEST_PATCH,
        "FAIL_TO_PASS": json.dumps(["tests/test_calc.py::test_add"]),
        "PASS_TO_PASS": json.dumps(["tests/test_calc.py::test_double"]),
    }
    return instance, fix
