"""
The git side of a benchmark task: a checkout that cannot see the answer.

Started life as Autonomous-SWE-Agent's `sandbox/workspace.py` (clone, baseline,
diff). Three of its behaviours were wrong for a benchmark and are replaced:

* **The clone kept the future.** `git clone` of the whole repository, then a
  checkout of the base commit, leaves every later commit — including the one
  that fixed the issue — in the object store, reachable from `origin/*` refs,
  and `origin` itself configured. An agent that runs `git log --all` or
  `git show origin/main:path` is reading the gold patch. Here the checkout is
  `git init` + a depth-1 fetch of exactly the base commit, from a URL rather
  than a named remote: no remote, no other refs, no history.
  `assert_isolated` checks this after every clone, and
  `tests/test_bench_checkout.py` proves the fix commit is unreachable.
* **The diff staged into the real index** (`git add -A` … `git reset`). It now
  uses a throwaway index file, as CodePilot's checkpoints already did.
* **There was no way back to a clean tree**, so grading ran the held-out tests
  in whatever state the agent left — including any `conftest.py` it wrote.
  `restore_pristine` returns the tree to the baseline exactly, and grading
  applies only the agent's filtered source diff on top (see `grading.py`).
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

CLONE_TIMEOUT = int(os.getenv("BENCH_CLONE_TIMEOUT", "900"))
GIT_IDENT = ["-c", "user.email=bench@codepilot.local", "-c", "user.name=codepilot-bench"]
#: The branch the checkout sits on. Any name works; it must simply not be one
#: an upstream might also use, so nothing about it suggests a remote.
BRANCH = "bench"


class CheckoutError(RuntimeError):
    """The checkout could not be created, or is not isolated."""


def git(
    cwd: Path | str,
    *args: str,
    check: bool = True,
    env: dict[str, str] | None = None,
    timeout: int = CLONE_TIMEOUT,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *GIT_IDENT, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env={**os.environ, **(env or {})},
    )
    if check and result.returncode != 0:
        raise CheckoutError(
            f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()[:500]}"
        )
    return result


def clone_at(repo_url: str, commit: str, dest: Path | str) -> str:
    """Create a checkout of exactly `commit` at `dest`, and return its sha.

    `git init` plus a depth-1 fetch by URL: the result has one commit, no
    remote, no tags and no other refs. Servers that refuse to serve a commit by
    id get a full clone into a temporary directory, from which the one commit
    is fetched the same way; the temporary clone is then deleted, so nothing
    from it survives in `dest`.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=False)
    git(dest, "init", "-q")
    fetched = git(dest, "fetch", "-q", "--depth=1", "--no-tags", repo_url, commit, check=False)
    if fetched.returncode != 0:
        with tempfile.TemporaryDirectory(prefix="bench-full-") as tmp:
            full = Path(tmp) / "full"
            git(tmp, "clone", "-q", "--no-checkout", repo_url, str(full))
            git(full, "config", "uploadpack.allowAnySHA1InWant", "true")
            git(dest, "fetch", "-q", "--depth=1", "--no-tags", full.as_uri(), commit)
    git(dest, "checkout", "-q", "-B", BRANCH, "FETCH_HEAD")
    # FETCH_HEAD names the URL it came from. Harmless, but nothing here needs
    # it, and "the checkout knows nothing about where it came from" is easier
    # to verify than "it knows, but only this much".
    (dest / ".git" / "FETCH_HEAD").unlink(missing_ok=True)
    sha = git(dest, "rev-parse", "HEAD").stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", commit) and sha != commit:
        raise CheckoutError(f"asked for {commit}, checked out {sha}")
    assert_isolated(dest)
    return sha


def assert_isolated(root: Path | str) -> None:
    """Refuse a checkout that can see anything beyond its own commits.

    No remotes, and every ref resolves into the current branch's history.
    """
    remotes = git(root, "remote").stdout.split()
    if remotes:
        raise CheckoutError(f"checkout has remotes configured: {remotes}")
    head_history = set(git(root, "rev-list", "HEAD").stdout.split())
    for line in git(root, "for-each-ref", "--format=%(objectname) %(refname)").stdout.splitlines():
        sha, ref = line.split(" ", 1)
        if sha not in head_history and not ref.startswith("refs/codepilot/"):
            raise CheckoutError(f"ref {ref} points outside the task's history")


def commit_baseline(root: Path | str, message: str = "bench baseline") -> str:
    """Commit the tree as it stands after setup, and return the commit.

    Setup leaves debris — `pip install -e .` alone drops an egg-info directory —
    and without a baseline every one of those files turns up in the agent's
    patch. Pinning a commit here means the final diff is exactly what the
    agent did.
    """
    git(root, "add", "-A")
    git(root, "commit", "-q", "--allow-empty", "--no-verify", "-m", message)
    return git(root, "rev-parse", "HEAD").stdout.strip()


def untracked(root: Path | str, ignored: bool) -> set[str]:
    args = ["ls-files", "--others", "--exclude-standard", "-z"]
    if ignored:
        args.insert(2, "--ignored")
    out = git(root, *args).stdout
    return {p for p in out.split("\0") if p}


def diff_since(root: Path | str, baseline: str) -> str:
    """Unified diff of everything changed since `baseline`, new files included.

    Built in a throwaway index so the checkout's own index is never touched.
    `git diff HEAD` alone silently omits untracked files, so a patch that adds
    a module would come back without it.
    """
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        git(root, "read-tree", baseline, env=env)
        git(root, "add", "-A", env=env)
        return git(root, "diff", "--cached", "--binary", baseline, env=env).stdout


def restore_pristine(root: Path | str, baseline: str, keep_ignored: set[str]) -> list[str]:
    """Return the working tree to `baseline` exactly. Returns what was removed.

    Tracked content is restored from the baseline commit (deleted files come
    back, edits are undone). Every untracked file is removed, and every
    *ignored* file that did not exist at baseline — an agent can write a
    `conftest.py` into a directory the repository ignores. Ignored files that
    were there at baseline (build artefacts of the install) are kept, or the
    environment the tests need would go with them.
    """
    root = Path(root)
    git(root, "restore", "--source", baseline, "--staged", "--worktree", "--", ".")
    removed: list[str] = []
    stray = untracked(root, ignored=False) | (untracked(root, ignored=True) - keep_ignored)
    for rel in sorted(stray):
        path = root / rel
        if path.is_file() or path.is_symlink():
            path.unlink()
            removed.append(rel)
    return removed


def apply_patch(root: Path | str, diff: str) -> tuple[bool, str]:
    """Apply a unified diff to the working tree. Returns (applied, detail)."""
    if not diff.strip():
        return True, "empty patch"
    with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False, encoding="utf-8",
                                     newline="\n") as fh:
        fh.write(diff if diff.endswith("\n") else diff + "\n")
        name = fh.name
    try:
        for extra in ([], ["--recount", "--ignore-whitespace"]):
            result = git(root, "apply", "--whitespace=nowarn", *extra, name, check=False)
            if result.returncode == 0:
                return True, "applied" + (" (whitespace-tolerant)" if extra else "")
        return False, (result.stderr or result.stdout).strip()[:1000]
    finally:
        Path(name).unlink(missing_ok=True)


@dataclass
class FilePatch:
    path: str
    text: str


def split_diff(diff: str) -> list[FilePatch]:
    """A multi-file unified diff, one entry per file, in order."""
    parts: list[FilePatch] = []
    current: list[str] = []
    path = ""
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current:
                parts.append(FilePatch(path, "".join(current)))
            current = [line]
            match = re.match(r"diff --git a/(.+?) b/(.+)$", line.rstrip("\n"))
            path = match.group(2) if match else ""
        else:
            current.append(line)
    if current:
        parts.append(FilePatch(path, "".join(current)))
    return [p for p in parts if p.path]


def changed_files(diff: str) -> list[str]:
    return [p.path for p in split_diff(diff)]


def changed_lines(diff: str) -> int:
    """Lines the patch adds or removes — not the diff's length.

    From Autonomous-SWE-Agent's loop: a one-line edit is a 13-line diff once
    headers, the hunk marker and context are counted.
    """
    return sum(
        1 for line in diff.splitlines()
        if re.match(r"^[+-][^+-]", line) and line[1:].strip()
    )
