"""The task checkout: the base commit and nothing after it.

Reproduction (docs/MERGE_DECISIONS.md, D9): Autonomous-SWE-Agent's
`clone_repo` on this same two-commit fixture left the fix commit reachable
(`git cat-file -e <fix>` succeeded), kept `origin` configured, and listed the
fix in `git log --all`. These tests fail on that implementation.
"""

from __future__ import annotations

import subprocess

import pytest

from codepilot.bench import checkout
from tests.benchfixture import make_task


def _git_ok(cwd, *args) -> bool:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True).returncode == 0


@pytest.fixture
def task(tmp_path):
    return make_task(tmp_path)


def test_the_gold_fix_commit_is_unreachable(tmp_path, task):
    instance, fix = task
    dest = tmp_path / "checkout"
    sha = checkout.clone_at(instance["repo_url"], instance["base_commit"], dest)
    assert sha == instance["base_commit"]
    assert not _git_ok(dest, "cat-file", "-e", fix), "the fix commit's object is present"
    log = subprocess.run(
        ["git", "log", "--all", "--format=%H %s"], cwd=dest, capture_output=True, text=True
    ).stdout
    assert fix not in log and "GOLD FIX" not in log
    assert "a + b" not in (dest / "calc.py").read_text()


def test_the_checkout_has_no_remote_and_no_trace_of_its_source(tmp_path, task):
    instance, _ = task
    dest = tmp_path / "checkout"
    checkout.clone_at(instance["repo_url"], instance["base_commit"], dest)
    remotes = subprocess.run(["git", "remote"], cwd=dest, capture_output=True, text=True).stdout
    assert remotes.strip() == ""
    assert not (dest / ".git" / "FETCH_HEAD").exists()
    config = (dest / ".git" / "config").read_text()
    assert "upstream" not in config and "file:" not in config


def test_a_checkout_that_can_see_a_remote_is_refused(tmp_path, task):
    instance, _ = task
    dest = tmp_path / "checkout"
    checkout.clone_at(instance["repo_url"], instance["base_commit"], dest)
    subprocess.run(["git", "remote", "add", "origin", instance["repo_url"]], cwd=dest, check=True)
    with pytest.raises(checkout.CheckoutError, match="remotes"):
        checkout.assert_isolated(dest)


def test_a_ref_outside_the_tasks_history_is_refused(tmp_path, task):
    instance, fix = task
    dest = tmp_path / "checkout"
    checkout.clone_at(instance["repo_url"], instance["base_commit"], dest)
    # Smuggle the future in the way a full clone would have.
    subprocess.run(
        ["git", "fetch", "-q", instance["repo_url"], f"{fix}:refs/remotes/origin/main"],
        cwd=dest, check=True, capture_output=True,
    )
    with pytest.raises(checkout.CheckoutError, match="outside"):
        checkout.assert_isolated(dest)


def test_the_diff_includes_new_files_and_leaves_the_index_alone(tmp_path, task):
    instance, _ = task
    dest = tmp_path / "checkout"
    checkout.clone_at(instance["repo_url"], instance["base_commit"], dest)
    baseline = checkout.commit_baseline(dest)
    (dest / "calc.py").write_text("changed\n", encoding="utf-8")
    (dest / "new_module.py").write_text("x = 1\n", encoding="utf-8")
    diff = checkout.diff_since(dest, baseline)
    assert "new_module.py" in diff and "calc.py" in diff
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=dest, capture_output=True, text=True
    ).stdout
    assert staged.strip() == "", "computing the diff staged the agent's work"


def test_the_diff_excludes_bytecode_the_test_run_left_behind(tmp_path, task):
    """Running the repository's tests writes `__pycache__`; that is not an edit.

    The agent's diff is applied by grading and published as its patch, so a
    `.pyc` captured here is not cosmetic: `drop_reason` does not drop it, which
    means a compiled module reaches the graded patch, and a base85 binary blob
    reaches the results file. Nothing in the repository ignores `__pycache__`
    unless the repository itself happens to, and the local fixture does not.

    The caches are written here directly rather than by running pytest, so the
    test does not depend on `PYTHONDONTWRITEBYTECODE` being unset in whatever
    shell it runs in -- with it set, a run of the real tests leaves no bytecode
    and this would pass without proving anything.
    """
    instance, _ = task
    dest = tmp_path / "checkout"
    checkout.clone_at(instance["repo_url"], instance["base_commit"], dest)
    baseline = checkout.commit_baseline(dest)

    (dest / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    for cache in (dest / "__pycache__", dest / "tests" / "__pycache__"):
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "calc.cpython-312.pyc").write_bytes(b"\x03\xf3\r\n\x00binary")
    (dest / ".pytest_cache" / "v" / "cache").mkdir(parents=True)
    (dest / ".pytest_cache" / "v" / "cache" / "lastfailed").write_text("{}", encoding="utf-8")

    diff = checkout.diff_since(dest, baseline)
    assert "calc.py" in diff, "the real edit must still be captured"
    assert "__pycache__" not in diff
    assert ".pyc" not in diff
    assert ".pytest_cache" not in diff
    assert "GIT binary patch" not in diff


def test_restore_returns_the_exact_baseline(tmp_path, task):
    instance, _ = task
    dest = tmp_path / "checkout"
    checkout.clone_at(instance["repo_url"], instance["base_commit"], dest)
    (dest / "build").mkdir()
    (dest / "build" / "artifact.txt").write_text("from setup", encoding="utf-8")
    baseline = checkout.commit_baseline(dest)
    keep = checkout.untracked(dest, ignored=True)

    (dest / "calc.py").write_text("broken\n", encoding="utf-8")
    (dest / "tests" / "test_calc.py").unlink()
    (dest / "stray.py").write_text("x\n", encoding="utf-8")
    (dest / "build" / "conftest.py").write_text("# ignored, but live\n", encoding="utf-8")

    removed = checkout.restore_pristine(dest, baseline, keep)
    assert (dest / "calc.py").read_text().startswith("def add")
    assert (dest / "tests" / "test_calc.py").is_file()
    assert not (dest / "stray.py").exists()
    assert not (dest / "build" / "conftest.py").exists()
    assert (dest / "build" / "artifact.txt").is_file(), "setup's ignored output must survive"
    assert set(removed) == {"stray.py", "build/conftest.py"}
    assert checkout.diff_since(dest, baseline) == ""


def test_changed_lines_counts_edits_not_diff_length():
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n a\n-b\n+c\n d\n"
    assert checkout.changed_lines(diff) == 2


def test_the_checkout_holds_the_repositorys_bytes_whatever_autocrlf_says(tmp_path, task):
    """On a Windows machine with core.autocrlf=true (this one), a plain
    checkout rewrites LF files as CRLF."""
    instance, _ = task
    dest = tmp_path / "checkout"
    checkout.clone_at(instance["repo_url"], instance["base_commit"], dest)
    assert b"\r\n" not in (dest / "calc.py").read_bytes()
