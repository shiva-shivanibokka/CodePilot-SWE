"""
Local workspace — the agent, with no Docker and no isolation.

This exists because the Docker sandbox is a hard prerequisite that a lot of
machines don't meet: no Docker Desktop, no nested virtualisation, a laptop that
can't spare the memory. Without an alternative, the whole project is unrunnable
for those users and unrecordable on that hardware, which is how a repo ends up
shipping a hand-written "sample" trace instead of a real one.

So the same agent runs against a temp directory on the host instead of a
container. Everything above this file — the loop, the three tools, the agentless
pipeline — is unchanged; only the execution backend differs.

**This is not a sandbox.** The model issues shell commands and they run as you,
on your machine, with your network and your filesystem. Use it on repositories
you already trust, and use DockerWorkspace for anything else. The guard rails
below (a working directory under the system temp dir, a hard timeout on every
command, and a refusal list for the handful of commands that are catastrophic
and never necessary) narrow the blast radius; they do not make it safe against a
model that is actively trying to escape, and nothing at this layer could.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from observability.tracing import get_tracer
from sandbox.workspace import CommandResult, SandboxError, clone_repo, commit_baseline, read_diff

tracer = get_tracer(__name__)

DEFAULT_CMD_TIMEOUT = int(os.getenv("SANDBOX_CMD_TIMEOUT", "120"))

# Same code coreutils `timeout` uses, so both backends report a kill identically.
TIMEOUT_EXIT_CODE = 124

# The agent is told the repo lives at /repo. Rewriting that prefix to the real
# temp path keeps one prompt, one set of tool descriptions and one recorded
# trace across both backends — the model never learns which one it is on.
VIRTUAL_ROOT = "/repo"

# "/repo" followed by one or more backslash-separated path segments.
_WINDOWS_TAIL = re.compile(r"/repo(?:\\[^\\\s:*?\"<>|]+)+")

# Commands that are unrecoverable and never legitimately needed to fix a bug.
# This is a seatbelt, not a sandbox: it stops an obvious accident, not an
# adversary. Anything genuinely untrusted belongs in DockerWorkspace.
_REFUSED = [
    (re.compile(r"\brm\s+(-[a-zA-Z]*\s+)*-?[a-zA-Z]*[rf]{2}[a-zA-Z]*\s+/(\s|$)"), "rm -rf /"),
    (re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f?\s+~"), "recursive delete of your home directory"),
    (re.compile(r"\b(mkfs|fdisk|dd)\b.*\bof=/dev/"), "writing to a raw device"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"), "shutting the machine down"),
    (re.compile(r"\bsudo\b"), "sudo"),
    (re.compile(r"\bcurl\b[^|]*\|\s*(ba)?sh\b"), "piping a download into a shell"),
    (re.compile(r"\bwget\b[^|]*\|\s*(ba)?sh\b"), "piping a download into a shell"),
    (re.compile(r":\(\)\s*\{.*\}\s*;?\s*:"), "a fork bomb"),
]


def refusal_reason(command: str) -> str | None:
    """Return why a command is refused, or None if it may run."""
    for pattern, description in _REFUSED:
        if pattern.search(command):
            return description
    return None


@dataclass
class LocalWorkspace:
    """
    A checkout of the target repo in a temp directory, driven by subprocess.

    Mirrors DockerWorkspace's interface exactly, so the agent cannot tell the
    difference — see sandbox.workspace.Workspace for the contract.

    Usage:
        with LocalWorkspace.create(repo_url, commit_sha) as ws:
            ws.run("pytest -q")
    """

    root: Path
    task_id: str
    repo_url: str
    commit_sha: str
    _tempdir: str | None = field(default=None, repr=False)
    _venv_bin: Path | None = field(default=None, repr=False)

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        repo_url: str,
        commit_sha: str,
        task_id: str | None = None,
        root: str | None = None,
    ) -> LocalWorkspace:
        """
        Clone the repo into a temp directory and prepare it for the agent.

        Args:
            repo_url:   HTTPS URL of the repository to clone.
            commit_sha: Exact commit to check out. "HEAD" for the default branch.
            task_id:    Optional identifier for tracing/logging.
            root:       Optional existing directory to use instead of a temp one.
        """
        task_id = task_id or str(uuid.uuid4())[:8]
        _bash()  # fail here, not thirty seconds into a clone

        with tracer.start_as_current_span("local.create") as span:
            span.set_attribute("task_id", task_id)
            span.set_attribute("repo_url", repo_url)
            span.set_attribute("commit_sha", commit_sha)

            tempdir = None
            if root:
                repo_path = Path(root)
                repo_path.mkdir(parents=True, exist_ok=True)
            else:
                tempdir = tempfile.mkdtemp(prefix=f"swe-agent-{task_id}-")
                repo_path = Path(tempdir) / "repo"

            ws = cls(
                root=repo_path,
                task_id=task_id,
                repo_url=repo_url,
                commit_sha=commit_sha,
                _tempdir=tempdir,
            )

            try:
                print(f"[local] Cloning {repo_url} -> {repo_path}")
                clone_repo(repo_url, commit_sha, str(repo_path))
                ws._install()
                commit_baseline(ws)
            except Exception:
                ws.teardown()
                raise
            return ws

    def _install(self) -> None:
        """
        Give the checkout its own virtualenv and install it there.

        Installing the target repo into the interpreter running the agent would
        mean every task permanently rewrites the agent's own dependency tree —
        one benchmark instance downgrading werkzeug for the next. The venv lives
        beside the checkout rather than inside it, so it never lands in a diff.
        """
        venv_dir = self.root.parent / "venv"
        created = subprocess.run(
            [sys.executable, "-m", "venv", str(venv_dir)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if created.returncode == 0:
            self._venv_bin = venv_dir / ("Scripts" if os.name == "nt" else "bin")
            print(f"[local] venv -> {venv_dir}")
        else:
            print(f"[local] venv creation failed, using the current interpreter: {created.stderr}")

        result = self.run("python -m pip install -e . --quiet", timeout=900)
        if not result.success:
            print(f"[local] editable install skipped (exit {result.exit_code}) - continuing")

    def setup(self, command: str, timeout: int = 1800) -> CommandResult:
        """
        Run an environment-preparation command before the agent starts.

        Old commits routinely need pins a modern interpreter would not pick on
        its own — this is where those go. It runs in the task's own venv and is
        recorded alongside the run, because an environment nobody can reproduce
        makes the result unreproducible too.
        """
        print(f"[local] setup: {command}")
        result = self.run(command, timeout=timeout)
        if not result.success:
            print(f"[local] setup exited {result.exit_code}:\n{result.output[-2000:]}")
        return result

    # ── Path translation ──────────────────────────────────────────────────────

    def to_host(self, path: str) -> Path:
        """Map an agent-visible path (/repo/...) onto the real host path."""
        if path == VIRTUAL_ROOT:
            return self.root
        if path.startswith(VIRTUAL_ROOT + "/"):
            return self.root / path[len(VIRTUAL_ROOT) + 1 :]
        candidate = Path(path)
        return candidate if candidate.is_absolute() else self.root / path

    @property
    def _shell_root(self) -> str:
        """The checkout as the shell addresses it (POSIX form, including on Windows)."""
        return _posix(self.root)

    def _rewrite(self, text: str) -> str:
        """Map host paths back to /repo so the model sees one consistent tree."""
        mapped = text.replace(self._shell_root, VIRTUAL_ROOT).replace(
            str(self.root), VIRTUAL_ROOT
        )
        # On Windows the host root is a backslash path, so replacing the prefix
        # alone leaves a mixed "/repo\src\app.py" - a path the model is told to
        # reuse verbatim, in a tree it has been told is POSIX.
        return _WINDOWS_TAIL.sub(lambda m: m.group(0).replace("\\", "/"), mapped)

    # ── Command execution ─────────────────────────────────────────────────────

    def run(
        self,
        command: str,
        timeout: int = DEFAULT_CMD_TIMEOUT,
        workdir: str = VIRTUAL_ROOT,
    ) -> CommandResult:
        """Run a shell command in the checkout. Never raises on a failing command."""
        with tracer.start_as_current_span("local.run") as span:
            span.set_attribute("command", command[:200])
            span.set_attribute("timeout", timeout)

            refused = refusal_reason(command)
            if refused:
                return CommandResult(
                    command=command,
                    stdout="",
                    stderr=(
                        f"Refused: this workspace does not permit {refused}. "
                        "It runs directly on the host, not in a container."
                    ),
                    exit_code=126,
                    duration_ms=0,
                    timeout_seconds=timeout,
                )

            cwd = self.to_host(workdir)
            translated = command.replace(VIRTUAL_ROOT, self._shell_root)
            t0 = time.monotonic()
            timed_out = False

            try:
                stdout, stderr, exit_code, timed_out = _run_with_deadline(
                    [_bash(), "-c", translated], cwd=str(cwd), env=self._env(), timeout=timeout
                )
            except OSError as exc:
                raise SandboxError(f"could not run command: {exc}") from exc

            duration_ms = int((time.monotonic() - t0) * 1000)
            result = CommandResult(
                command=command,
                stdout=self._rewrite(stdout),
                stderr=self._rewrite(stderr),
                exit_code=exit_code,
                timed_out=timed_out,
                duration_ms=duration_ms,
                timeout_seconds=timeout,
            )

            span.set_attribute("exit_code", exit_code)
            span.set_attribute("duration_ms", duration_ms)
            span.set_attribute("timed_out", timed_out)
            return result

    def _env(self) -> dict[str, str]:
        """
        The child's environment, minus every provider API key.

        The agent's own key reaches the model through the LLM client, never
        through the shell — so a command that dumps the environment into the
        transcript can't leak it back into the model's context or a recording.
        """
        env = dict(os.environ)
        for name in list(env):
            if name.endswith(("_API_KEY", "_TOKEN")) or name.endswith("_SECRET"):
                env.pop(name, None)

        env["PYTHONIOENCODING"] = "utf-8"
        env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"

        # Put the task's own venv first so `python`, `pip` and `pytest` in the
        # model's commands mean this checkout's interpreter, not the agent's.
        if self._venv_bin:
            env["VIRTUAL_ENV"] = str(self._venv_bin.parent)
            env["PATH"] = f"{self._venv_bin}{os.pathsep}{env.get('PATH', '')}"
            env.pop("PYTHONHOME", None)
        return env

    # ── File I/O ──────────────────────────────────────────────────────────────

    def read_file(self, path: str) -> str:
        target = self.to_host(path)
        try:
            return target.read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, IsADirectoryError, PermissionError) as exc:
            raise FileNotFoundError(f"Cannot read {path}: {exc}") from exc

    def write_file(self, path: str, content: str) -> None:
        target = self.to_host(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")

    def file_exists(self, path: str) -> bool:
        return self.to_host(path).is_file()

    def list_files(self, directory: str = VIRTUAL_ROOT) -> list[str]:
        base = self.to_host(directory)
        if not base.is_dir():
            return []
        return sorted(
            f"{VIRTUAL_ROOT}/{p.relative_to(self.root).as_posix()}"
            for p in base.rglob("*")
            if p.is_file() and ".git" not in p.parts
        )

    def get_diff(self) -> str:
        return read_diff(self)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def teardown(self) -> None:
        """Delete the checkout. Only ever removes a directory this class created."""
        with tracer.start_as_current_span("local.teardown"):
            if self._tempdir:
                shutil.rmtree(self._tempdir, ignore_errors=True)
                self._tempdir = None

    def __enter__(self) -> LocalWorkspace:
        return self

    def __exit__(self, *args) -> None:
        self.teardown()


def _run_with_deadline(
    argv: list[str], *, cwd: str, env: dict[str, str], timeout: int
) -> tuple[str, str, int, bool]:
    """
    Run a command and kill its whole process tree if it overruns.

    `subprocess.run(timeout=...)` only terminates the process it started. The
    agent's commands go through a shell, so the thing that actually hangs — a
    test suite, a server left in the foreground — is a grandchild that survives,
    keeps the stdout pipe open, and leaves the read blocking long past the
    deadline. A timeout that does not stop anything is worse than none, because
    everything above assumes it works.
    """
    kwargs: dict = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True  # its own process group to signal
    else:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        **kwargs,
    )

    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return stdout, stderr, process.returncode, False
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        stdout, stderr = process.communicate()
        return stdout or "", stderr or "", TIMEOUT_EXIT_CODE, True


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


def _bash() -> str:
    """
    Path to a POSIX shell.

    Every tool description in this repo tells the model it has bash — absolute
    paths, pipelines, `sed -n '10,50p'`. Handing those to cmd.exe on Windows
    would fail in ways that look like the model getting it wrong, so this is a
    hard requirement rather than a fallback. Git for Windows ships one.
    """
    found = shutil.which("bash")
    if not found:
        raise SandboxError(
            "LocalWorkspace needs bash on PATH (the agent's tools are POSIX shell "
            "commands). On Windows, install Git for Windows — it ships Git Bash. "
            "Alternatively use the Docker backend: WORKSPACE_BACKEND=docker."
        )
    return found


def _posix(path: Path) -> str:
    """Render a path the way the shell will read it, incl. MSYS-style on Windows."""
    text = str(path).replace("\\", "/")
    if len(text) > 1 and text[1] == ":":
        return f"/{text[0].lower()}{text[2:]}"
    return text


