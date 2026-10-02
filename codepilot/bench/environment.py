"""
One benchmark task's environment: an isolated checkout plus somewhere to run it.

This is the single workspace interface the benchmark uses, replacing
Autonomous-SWE-Agent's two parallel backends (`LocalWorkspace`,
`DockerWorkspace`) and their shared `/repo` path convention. Both backends now
share one design:

* the **checkout lives on the host** (`checkout.clone_at`: the base commit
  only, no remote, no history), so CodePilot's `Workspace` — read-before-write,
  containment, `.gitignore`-aware listing — works on it unchanged, and the diff,
  restore and grading steps are the same git operations whichever backend ran
  the commands;
* **commands run through CodePilot's `Sandbox` protocol**, the same one the
  agent's `run_command` / `run_tests` tools already use:

  - `local`: `LocalSandbox` with a per-task virtualenv first on PATH, a POSIX
    shell, and provider keys scrubbed from the child. **No isolation** — the
    model's commands run as you. For trusted repositories, or a machine with no
    Docker.
  - `docker`: `DockerSandbox` with the checkout bind-mounted. The container
    starts with a network so setup can install dependencies, and is taken off
    every network before the agent's first turn. With an official SWE-bench
    image (`swebench/sweb.eval.x86_64.<id>`) the checkout is mounted over the
    image's `/testbed` and the image's prepared conda environment is used.

The setup command, its exit code and the backend are recorded with every
result: an environment nobody can reproduce makes the result unreproducible.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from codepilot.bench import checkout
from codepilot.sandbox.base import CommandResult, Sandbox
from codepilot.sandbox.local import LocalSandbox

#: Official SWE-bench images keep the repository at /testbed and their
#: environment in a conda env named "testbed".
SWEBENCH_WORKDIR = "/testbed"
#: Built from deploy/bench.Dockerfile. Distinct from the hosted mode's
#: codepilot-sandbox image, which is deliberately minimal.
BENCH_IMAGE = "codepilot-bench:latest"
#: Commands run under `sh`, where conda's activate script does not work, so
#: the environment is selected by putting its bin directory first on PATH.
SWEBENCH_PREFIX = "export PATH=/opt/miniconda3/envs/testbed/bin:$PATH && "


def swebench_image(instance_id: str) -> str:
    """The official per-instance image name, as SWE-bench publishes them."""
    return f"swebench/sweb.eval.x86_64.{instance_id.replace('__', '_1776_')}:latest".lower()


@dataclass
class SetupRecord:
    command: str
    exit_code: int
    output_tail: str


@dataclass
class BenchEnv:
    root: Path
    base_commit: str
    baseline: str
    sandbox: Sandbox
    backend: str
    image: str = ""
    setup: list[SetupRecord] = field(default_factory=list)
    _keep_ignored: set[str] = field(default_factory=set)
    _tmpdir: Path | None = None

    # ------------------------------------------------------------ lifecycle

    @classmethod
    async def create(
        cls,
        repo_url: str,
        commit: str,
        *,
        backend: str = "local",
        setup: str | None = None,
        install: bool = True,
        image: str | None = None,
        python: str | None = None,
        venv: bool = True,
        task_id: str | None = None,
    ) -> BenchEnv:
        """Check `repo_url` out at `commit` and prepare it to run tests.

        Args:
            backend: "local" or "docker".
            setup:   command run after the install and before the baseline is
                     committed — dependency pins for an old commit, say.
            install: `pip install -e .` first (skipped for official SWE-bench
                     images, which are already installed).
            image:   Docker image; default CodePilot's sandbox image.
            python:  interpreter the local backend's virtualenv is built from.
            venv:    local backend only. False runs in the given (or this)
                     interpreter with no virtualenv — for tests and for
                     fixture repositories that need nothing installed.
        """
        if backend not in ("local", "docker"):
            raise ValueError(f"unknown backend {backend!r}: choose local or docker")
        tmpdir = Path(tempfile.mkdtemp(prefix=f"bench-{(task_id or uuid.uuid4().hex)[:24]}-"))
        root = tmpdir / "repo"
        try:
            base = checkout.clone_at(repo_url, commit, root)
            if backend == "local":
                sandbox = _local_sandbox(root, tmpdir, python, venv)
            else:
                sandbox = await _docker_sandbox(root, image)
            env = cls(
                root=root, base_commit=base, baseline=base, sandbox=sandbox,
                backend=backend, image=image or "", _tmpdir=tmpdir,
            )
            official = bool(image and image.startswith("swebench/"))
            commands = []
            if install and not official:
                commands.append("python -m pip install -q -e .")
            if setup:
                commands.append(setup)
            for command in commands:
                result = await sandbox.run(command, timeout_seconds=1800)
                env.setup.append(
                    SetupRecord(command, result.exit_code, result.combined[-2000:])
                )
            if backend == "docker":
                await sandbox.disconnect_network()  # type: ignore[attr-defined]
            env.baseline = checkout.commit_baseline(root)
            env._keep_ignored = checkout.untracked(root, ignored=True)
            return env
        except BaseException:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise

    async def close(self) -> None:
        try:
            await self.sandbox.close()
        finally:
            if self._tmpdir is not None:
                shutil.rmtree(self._tmpdir, ignore_errors=True)
                self._tmpdir = None

    async def __aenter__(self) -> BenchEnv:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # -------------------------------------------------------------- the tree

    def diff(self) -> str:
        """Everything changed since the baseline, new files included."""
        return checkout.diff_since(self.root, self.baseline)

    def restore(self) -> list[str]:
        """Back to the baseline exactly. Returns the files removed."""
        return checkout.restore_pristine(self.root, self.baseline, self._keep_ignored)

    def apply(self, diff: str) -> tuple[bool, str]:
        return checkout.apply_patch(self.root, diff)

    async def run(self, command: str, timeout: int = 600) -> CommandResult:
        return await self.sandbox.run(command, timeout_seconds=timeout)


def _local_sandbox(root: Path, tmpdir: Path, python: str | None, use_venv: bool) -> LocalSandbox:
    """A virtualenv beside the checkout (never inside it, so never in a diff)."""
    if not use_venv:
        return LocalSandbox(
            root=root, python=python or sys.executable, shell=_bash(), scrub_secrets=True
        )
    venv = tmpdir / "venv"
    subprocess.run(
        [python or sys.executable, "-m", "venv", str(venv)],
        check=True, capture_output=True, timeout=600,
    )
    bindir = venv / ("Scripts" if os.name == "nt" else "bin")
    interpreter = bindir / ("python.exe" if os.name == "nt" else "python")
    return LocalSandbox(
        root=root,
        python=str(interpreter),
        shell=_bash(),
        path_prefix=[str(bindir)],
        scrub_secrets=True,
        env={"VIRTUAL_ENV": str(venv), "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
    )


async def _docker_sandbox(root: Path, image: str | None) -> Sandbox:
    from codepilot.sandbox.docker import DockerSandbox

    image = image or BENCH_IMAGE
    official = image.startswith("swebench/")
    sandbox = DockerSandbox(
        image,
        memory="4g",
        cpus=2,
        network="bridge",
        mount=root,
        workdir=SWEBENCH_WORKDIR if official else "/workspace",
        prefix=SWEBENCH_PREFIX if official else "",
    )
    await sandbox.start()
    return sandbox


def _bash() -> str:
    """A POSIX shell for the local backend.

    The agent writes POSIX shell; handing that to cmd.exe fails in ways that
    look like the model's mistakes. Git for Windows ships bash.
    """
    found = shutil.which("bash")
    if os.name == "nt" and found and ("windowsapps" in found.lower() or "system32" in found.lower()):
        # That is WSL's launcher: a different filesystem view, not Git Bash.
        git_bash = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"
        found = str(git_bash) if git_bash.is_file() else None
    if not found:
        raise RuntimeError(
            "the local benchmark backend needs bash on PATH (on Windows: Git for "
            "Windows ships it), or use --backend docker"
        )
    return found
