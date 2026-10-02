"""
Runs commands in the real working directory.

The tool's backend. Safety here comes from the permission gate upstream, not
from isolation — the code being run is yours, in your repo, at your request.

The benchmark uses it too, as the no-Docker backend, against a throwaway
checkout of a third-party repository. **That is not a sandbox**: the model's
commands run as you, with your network and filesystem. Use the Docker backend
for anything you would not run yourself.

Windows matters: this project's author develops on it, so the shell is
resolved rather than assumed, a bare `python` is pointed at a known
interpreter (which may not otherwise exist, or may be the wrong one), and
output is decoded with a replacement policy so a stray byte cannot kill a run.

Three behaviours ported from Autonomous-SWE-Agent's local backend
(`sandbox/local_workspace.py`), each with a test in
`tests/test_local_sandbox.py`:

* a timeout kills the whole process tree, not just the shell;
* `scrub_secrets` removes `*_API_KEY`, `*_TOKEN` and `*_SECRET` variables from
  the child, so a command that prints its environment cannot leak a key into
  the transcript;
* `shell` can require a POSIX shell (Git Bash on Windows), because SWE-bench
  agents write POSIX shell.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from codepilot.sandbox.base import CommandResult
from codepilot.sandbox.pytest_parse import TestOutcome, parse

#: GNU timeout's exit code when it kills the child, so both backends report a
#: kill the same way.
TIMEOUT_EXIT = 124

SECRET_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET")


def scrubbed(env: dict[str, str]) -> dict[str, str]:
    """`env` minus every variable shaped like a credential."""
    return {k: v for k, v in env.items() if not k.upper().endswith(SECRET_SUFFIXES)}


class LocalSandbox:
    """Implements `Sandbox` against the host, in `root`."""

    def __init__(
        self,
        root: Path | str,
        env: dict[str, str] | None = None,
        *,
        python: str | None = None,
        shell: str | None = None,
        path_prefix: list[str] | None = None,
        scrub_secrets: bool = False,
    ) -> None:
        """
        Args:
            root:          working directory for every command.
            env:           extra variables for the child.
            python:        interpreter a bare `python`/`pytest` resolves to.
                           Default: the one running this process.
            shell:         a shell executable to run commands with (`bash`),
                           instead of the platform's default shell.
            path_prefix:   directories put first on the child's PATH (a
                           virtualenv's bin directory, say).
            scrub_secrets: drop credential-shaped variables from the child.
        """
        self.root = Path(root).resolve()
        base = dict(os.environ)
        if scrub_secrets:
            base = scrubbed(base)
        self._env = {**base, **(env or {})}
        # Unbuffered and UTF-8: without these, a child process's output arrives
        # in one lump at exit and non-ASCII arrives mangled on a cp1252 console.
        self._env.setdefault("PYTHONUNBUFFERED", "1")
        self._env.setdefault("PYTHONIOENCODING", "utf-8")
        if path_prefix:
            self._env["PATH"] = os.pathsep.join(
                [*path_prefix, self._env.get("PATH", "")]
            )
        self._python = python or sys.executable
        self._shell = shell

    def _normalise(self, command: str) -> str:
        """Point a bare `python`/`pytest` at the configured interpreter.

        On Windows `python` frequently resolves to a store stub, and inside a
        virtualenv a bare `pytest` may be a different environment's. Both
        produce failures that look like the agent's fault.
        """
        try:
            parts = shlex.split(command, posix=False)
        except ValueError:
            return command
        if not parts:
            return command
        head = parts[0].strip('"')
        python = self._python.replace("\\", "/") if self._shell else self._python
        if head in ("python", "python3", "py"):
            parts[0] = f'"{python}"'
        elif head == "pytest":
            parts[0:1] = [f'"{python}"', "-m", "pytest"]
        else:
            return command
        return " ".join(parts)

    async def run(self, command: str, timeout_seconds: int = 60) -> CommandResult:
        """Run to completion in a worker thread.

        Deliberately a thread rather than `asyncio.create_subprocess_shell`: on
        Windows the latter raises NotImplementedError under a SelectorEventLoop,
        which is what several async test runners install. Depending on the
        caller's event-loop policy to be able to run a command is a fragile way
        to build a coding agent, and nothing here streams output anyway.
        """
        started = time.monotonic()
        actual = self._normalise(command)
        argv: list[str] | str = [self._shell, "-c", actual] if self._shell else actual

        def _run() -> tuple[int, str, str, bool]:
            try:
                return _run_with_deadline(
                    argv,
                    shell=self._shell is None,
                    cwd=str(self.root),
                    env=self._env,
                    timeout=timeout_seconds,
                )
            except OSError as exc:
                return -1, "", f"could not start command: {exc}", False

        code, out, err, timed_out = await asyncio.to_thread(_run)
        if timed_out:
            err += f"\n[timed out after {timeout_seconds}s]"
        return CommandResult(
            command=command,
            stdout=out,
            stderr=err,
            exit_code=code,
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=timed_out,
        )

    async def run_tests(
        self, command: str = "pytest -q", timeout_seconds: int = 300
    ) -> TestOutcome:
        result = await self.run(command, timeout_seconds=timeout_seconds)
        outcome = parse(
            result.combined,
            result.exit_code,
            command=command,
            duration_ms=result.duration_ms,
        )
        outcome.timed_out = result.timed_out
        return outcome

    async def close(self) -> None:
        """Nothing to release: no container, no temp directory, no connection."""
        return None


def _run_with_deadline(
    argv: list[str] | str,
    *,
    shell: bool,
    cwd: str,
    env: dict[str, str],
    timeout: int,
) -> tuple[int, str, str, bool]:
    """Run a command and kill its whole process tree if it overruns.

    `subprocess.run(timeout=...)` only terminates the process it started. A
    command goes through a shell, so the thing that actually hangs — a test
    suite, a server left in the foreground — is a grandchild that survives,
    keeps the stdout pipe open, and leaves the read blocking long past the
    deadline. A timeout that does not stop anything is worse than none,
    because everything above assumes it works.
    """
    kwargs: dict = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True  # its own process group to signal
    else:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    process = subprocess.Popen(  # noqa: S602 - a shell is the point
        argv,
        shell=shell,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **kwargs,
    )
    try:
        out, err = process.communicate(timeout=timeout)
        timed_out = False
        code = process.returncode
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        out, err = process.communicate()
        timed_out = True
        code = TIMEOUT_EXIT
    return (
        code,
        (out or b"").decode("utf-8", errors="replace"),
        (err or b"").decode("utf-8", errors="replace"),
        timed_out,
    )


def _kill_tree(process: subprocess.Popen) -> None:
    """Kill a process and everything it spawned, on either platform."""
    if os.name == "posix":
        import signal

        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError):
            pass
    else:
        # Windows has no process groups to signal; taskkill /T walks the tree.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
    with contextlib.suppress(Exception):
        process.kill()
