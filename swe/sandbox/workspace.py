"""
The workspace contract shared by every execution backend.

The agent never touches a container or a directory directly — it goes through a
Workspace. That seam is what lets the same loop, the same tools and the same
agentless pipeline run against either backend:

    DockerWorkspace  — one throwaway, network-less container per task. The real
                       thing: safe against arbitrary code from an arbitrary repo.
    LocalWorkspace   — a temp directory on this machine. No isolation. Exists so
                       the agent can be run and recorded on a machine with no
                       Docker, on a repo you already trust.

Both raise the same errors and return the same CommandResult, so nothing
downstream needs to know which one it has.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

CLONE_TIMEOUT = int(os.getenv("SANDBOX_CLONE_TIMEOUT", "600"))


class SandboxError(Exception):
    """Raised when a workspace operation fails unrecoverably."""


class CommandTimeout(SandboxError):
    """Raised when a command exceeds its timeout and could not be killed."""


@dataclass
class CommandResult:
    """Result of a shell command executed inside a workspace."""

    command: str
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False
    duration_ms: int = 0
    timeout_seconds: int = 0

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def output(self) -> str:
        """Combined stdout + stderr, same as a terminal."""
        combined = []
        if self.stdout.strip():
            combined.append(self.stdout)
        if self.stderr.strip():
            combined.append(self.stderr)
        return "\n".join(combined).strip()

    def __str__(self) -> str:
        status = "OK" if self.success else f"EXIT={self.exit_code}"
        if self.timed_out:
            status = "TIMEOUT"
        return f"[{status}] {self.command!r}\n{self.output}"


@runtime_checkable
class Workspace(Protocol):
    """What the agent, the tools and the agentless pipeline require of a backend."""

    task_id: str
    repo_url: str
    commit_sha: str

    def run(self, command: str, timeout: int = ..., workdir: str = ...) -> CommandResult: ...

    def setup(self, command: str, timeout: int = ...) -> CommandResult: ...

    def read_file(self, path: str) -> str: ...

    def write_file(self, path: str, content: str) -> None: ...

    def file_exists(self, path: str) -> bool: ...

    def list_files(self, directory: str = ...) -> list[str]: ...

    def get_diff(self) -> str: ...

    def teardown(self) -> None: ...


# ── Shared helpers (used by both backends) ────────────────────────────────────


def _sh_quote(path: str) -> str:
    """Single-quote a path for the shell, escaping any embedded single quotes."""
    return "'" + path.replace("'", "'\\''") + "'"


def clone_repo(repo_url: str, commit_sha: str, dest: str) -> None:
    """
    Clone `repo_url` into `dest` and check out `commit_sha`.

    A shallow clone only carries recent history, and SWE-bench base commits are
    routinely years deep — so a shallow checkout of the target commit fails on
    most instances. Fetch the exact commit instead, and only fall back to
    deepening the whole history if the server refuses single-commit fetches.
    """
    run = _git_runner(dest)

    result = subprocess.run(
        ["git", "clone", "--filter=blob:none", "--no-checkout", repo_url, dest],
        capture_output=True,
        text=True,
        timeout=CLONE_TIMEOUT,
    )
    if result.returncode != 0:
        raise SandboxError(f"Failed to clone {repo_url}: {result.stderr.strip()}")

    if commit_sha and commit_sha != "HEAD":
        fetched = run("fetch", "--depth=1", "origin", commit_sha)
        if fetched.returncode != 0:
            # Server disallows fetching an arbitrary SHA; take the full history.
            run("fetch", "--unshallow")

    checkout = run("checkout", "--force", commit_sha or "HEAD")
    if checkout.returncode != 0:
        raise SandboxError(
            f"Failed to check out {commit_sha!r} in {repo_url}: {checkout.stderr.strip()}"
        )


def _git_runner(cwd: str):
    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=CLONE_TIMEOUT
        )

    return run


GIT_IDENT = "-c user.email=agent@swe.local -c user.name=swe-agent"


def commit_baseline(workspace) -> None:
    """
    Commit the state of the repo the moment before the agent starts.

    Setting the workspace up leaves debris — `pip install -e .` alone drops an
    egg-info directory — and without a baseline every one of those files turns up
    in the agent's patch. Pinning HEAD here means the final diff is exactly what
    the agent did and nothing else.
    """
    workspace.run(f"git {GIT_IDENT} add -A")
    workspace.run(f"git {GIT_IDENT} commit -q --allow-empty -m 'workspace baseline'")


def read_diff(workspace) -> str:
    """
    Return the unified diff of everything the agent changed.

    Staged first so files the agent *created* are included: `git diff HEAD`
    alone silently omits untracked files, so a patch that adds a new module
    comes back empty — which reads downstream as "the agent changed nothing".
    """
    workspace.run(f"git {GIT_IDENT} add -A")
    result = workspace.run("git diff --cached HEAD")
    workspace.run("git reset -q")
    if not result.success and not result.stdout.strip():
        raise SandboxError(f"git diff failed in workspace: {result.output[:500]}")
    return result.stdout
