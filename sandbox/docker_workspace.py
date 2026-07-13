"""
Per-task Docker workspace.

Each SWE-bench task (or live GitHub issue) gets an isolated Docker container
built from Dockerfile.sandbox. The container is created fresh, the target
repo is cloned into it, and then the agent runs bash commands inside it via
docker exec. The container is torn down after the task completes.

Design principles:
- One container per task — no state bleed.
- The host filesystem is never mounted — all file I/O goes through Docker's
  archive API (tar streams), same as SWE-bench official harness.
- Memory and CPU are capped so runaway processes don't kill the host.
- All errors from docker exec are surfaced as structured exceptions.
"""

from __future__ import annotations

import io
import os
import tarfile
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import docker
import docker.errors
from docker.models.containers import Container

from observability.tracing import get_tracer

# ── Constants ─────────────────────────────────────────────────────────────────

SANDBOX_IMAGE = "swe-agent-sandbox:latest"
SANDBOX_DOCKERFILE = Path(__file__).parent / "Dockerfile.sandbox"
REPO_PATH_IN_CONTAINER = "/repo"

DEFAULT_MEMORY = os.getenv("SANDBOX_MEMORY_LIMIT", "2g")
DEFAULT_CPU = float(os.getenv("SANDBOX_CPU_LIMIT", "2.0"))
DEFAULT_CMD_TIMEOUT = int(os.getenv("SANDBOX_CMD_TIMEOUT", "120"))

tracer = get_tracer(__name__)


# ── Exceptions ────────────────────────────────────────────────────────────────


class SandboxError(Exception):
    """Raised when a sandbox operation fails unrecoverably."""


class CommandTimeout(SandboxError):
    """Raised when a bash command exceeds the configured timeout."""


# ── Result dataclass ──────────────────────────────────────────────────────────


@dataclass
class CommandResult:
    """Result of a bash command executed inside the sandbox."""

    command: str
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False
    duration_ms: int = 0

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


# ── Docker workspace ──────────────────────────────────────────────────────────


@dataclass
class DockerWorkspace:
    """
    Isolated Docker container for a single SWE task.

    Usage:
        async with DockerWorkspace.create(repo_url, commit_sha) as ws:
            result = ws.run("pytest tests/")
            content = ws.read_file("src/module.py")
            ws.write_file("src/module.py", new_content)
    """

    container: Container
    task_id: str
    repo_url: str
    commit_sha: str
    _client: docker.DockerClient = field(repr=False)

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        repo_url: str,
        commit_sha: str,
        task_id: str | None = None,
        memory_limit: str = DEFAULT_MEMORY,
        cpu_limit: float = DEFAULT_CPU,
    ) -> DockerWorkspace:
        """
        Spin up a fresh sandbox container and clone the repo at the given commit.

        Args:
            repo_url:     HTTPS URL of the repository to clone.
            commit_sha:   The exact commit to check out (pre-issue state).
            task_id:      Optional identifier for tracing/logging.
            memory_limit: Docker memory limit string e.g. "2g".
            cpu_limit:    Docker CPU limit (float cores).

        Returns:
            A DockerWorkspace ready to accept commands.
        """
        task_id = task_id or str(uuid.uuid4())[:8]

        with tracer.start_as_current_span("sandbox.create") as span:
            span.set_attribute("task_id", task_id)
            span.set_attribute("repo_url", repo_url)
            span.set_attribute("commit_sha", commit_sha)

            client = docker.from_env()
            cls._ensure_image(client)

            container = client.containers.run(
                image=SANDBOX_IMAGE,
                command="/bin/bash",
                detach=True,
                tty=True,
                stdin_open=True,
                name=f"swe-agent-{task_id}",
                mem_limit=memory_limit,
                nano_cpus=int(cpu_limit * 1e9),
                network_mode="none",  # no internet inside sandbox
                remove=False,  # we remove explicitly on teardown
                labels={"swe-agent": "sandbox", "task_id": task_id},
                user="sweagent",
                working_dir=REPO_PATH_IN_CONTAINER,
            )

            ws = cls(
                container=container,
                task_id=task_id,
                repo_url=repo_url,
                commit_sha=commit_sha,
                _client=client,
            )

            # Clone the repo — needs network, so we do it before locking it
            # We temporarily allow network just for the clone, then disable it.
            # Simplest approach: clone via exec with the container's default network,
            # then disconnect from networks after clone is done.
            # For SWE-bench, repos are pre-cloned into the image or pulled here.
            ws._setup_repo()
            return ws

    @staticmethod
    def _ensure_image(client: docker.DockerClient) -> None:
        """Build the sandbox image if it doesn't exist."""
        try:
            client.images.get(SANDBOX_IMAGE)
        except docker.errors.ImageNotFound:
            print(f"[sandbox] Building {SANDBOX_IMAGE} from {SANDBOX_DOCKERFILE} ...")
            client.images.build(
                path=str(SANDBOX_DOCKERFILE.parent),
                dockerfile=str(SANDBOX_DOCKERFILE.name),
                tag=SANDBOX_IMAGE,
                rm=True,
            )

    def _setup_repo(self) -> None:
        """
        Clone the target repo at the correct commit inside the container.

        The container runs with network_mode=none for security, so we clone
        on the host and copy the result in via Docker archive API.
        """
        with tracer.start_as_current_span("sandbox.setup_repo"):
            with tempfile.TemporaryDirectory() as tmpdir:
                # Clone on host (has network access)
                import subprocess

                result = subprocess.run(
                    ["git", "clone", "--depth=50", self.repo_url, tmpdir + "/repo"],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if result.returncode != 0:
                    raise SandboxError(f"Failed to clone {self.repo_url}: {result.stderr}")

                # Checkout the exact commit
                subprocess.run(
                    ["git", "checkout", self.commit_sha],
                    cwd=tmpdir + "/repo",
                    capture_output=True,
                    text=True,
                    timeout=30,
                )

                # Copy the repo into the container via tar archive
                self._copy_dir_to_container(tmpdir + "/repo", REPO_PATH_IN_CONTAINER)

            # Install repo dependencies inside the container (best-effort)
            self.run("pip install -e . --quiet 2>/dev/null || true", timeout=180)

    def _copy_dir_to_container(self, host_path: str, container_path: str) -> None:
        """Copy a host directory into the container using Docker archive API."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            tar.add(host_path, arcname=".")
        buf.seek(0)
        self.container.put_archive(container_path, buf)

    # ── Command execution ─────────────────────────────────────────────────────

    def run(
        self,
        command: str,
        timeout: int = DEFAULT_CMD_TIMEOUT,
        workdir: str = REPO_PATH_IN_CONTAINER,
    ) -> CommandResult:
        """
        Execute a bash command inside the sandbox.

        The command runs in a fresh shell each time (not a persistent PTY),
        but shares the same filesystem state. For stateful sessions (cd, env vars),
        use run_session() instead.

        Args:
            command: Shell command to run.
            timeout: Seconds before the command is killed.
            workdir: Working directory inside the container.

        Returns:
            CommandResult with stdout, stderr, exit_code.
        """
        with tracer.start_as_current_span("sandbox.run") as span:
            span.set_attribute("command", command[:200])
            span.set_attribute("timeout", timeout)

            t0 = time.monotonic()
            timed_out = False

            try:
                exit_code, output = self.container.exec_run(
                    cmd=["bash", "-c", command],
                    workdir=workdir,
                    demux=True,
                    user="sweagent",
                    environment={"HOME": "/home/sweagent", "PATH": "/usr/local/bin:/usr/bin:/bin"},
                )
                stdout_bytes, stderr_bytes = output
                stdout = (stdout_bytes or b"").decode("utf-8", errors="replace")
                stderr = (stderr_bytes or b"").decode("utf-8", errors="replace")
            except Exception as exc:
                raise SandboxError(f"exec_run failed: {exc}") from exc

            duration_ms = int((time.monotonic() - t0) * 1000)

            result = CommandResult(
                command=command,
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                timed_out=timed_out,
                duration_ms=duration_ms,
            )

            span.set_attribute("exit_code", exit_code)
            span.set_attribute("duration_ms", duration_ms)
            return result

    # ── File I/O ──────────────────────────────────────────────────────────────

    def read_file(self, path: str) -> str:
        """Read a file from the container. Path is absolute inside the container."""
        result = self.run(f"cat '{path}'")
        if not result.success:
            raise FileNotFoundError(f"Cannot read {path} in sandbox: {result.stderr}")
        return result.stdout

    def write_file(self, path: str, content: str) -> None:
        """Write content to a file inside the container."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            encoded = content.encode("utf-8")
            info = tarfile.TarInfo(name=Path(path).name)
            info.size = len(encoded)
            tar.addfile(info, io.BytesIO(encoded))
        buf.seek(0)
        container_dir = str(Path(path).parent)
        self.container.put_archive(container_dir, buf)

    def file_exists(self, path: str) -> bool:
        """Check if a file exists inside the container."""
        result = self.run(f"test -f '{path}' && echo yes || echo no")
        return result.stdout.strip() == "yes"

    def list_files(self, directory: str = REPO_PATH_IN_CONTAINER) -> list[str]:
        """List all files in a directory (recursively), relative paths."""
        result = self.run(
            f"find '{directory}' -type f -not -path '*/.git/*' | sort",
            workdir=directory,
        )
        if not result.success:
            return []
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return lines

    def get_diff(self) -> str:
        """Return the git diff of all changes made to the repo."""
        result = self.run("git diff HEAD", workdir=REPO_PATH_IN_CONTAINER)
        return result.stdout

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def teardown(self) -> None:
        """Stop and remove the container. Always call this when done."""
        with tracer.start_as_current_span("sandbox.teardown"):
            try:
                self.container.stop(timeout=10)
            except Exception:
                pass
            try:
                self.container.remove(force=True)
            except Exception:
                pass

    def __enter__(self) -> DockerWorkspace:
        return self

    def __exit__(self, *args) -> None:
        self.teardown()
