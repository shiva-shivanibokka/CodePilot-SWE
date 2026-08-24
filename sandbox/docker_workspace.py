"""
Per-task Docker workspace — the real sandbox.

Each SWE-bench task (or live GitHub issue) gets an isolated container built from
Dockerfile.sandbox. The container is created fresh, the target repo is copied in,
and the agent runs bash commands inside it via docker exec. The container is torn
down after the task completes.

Design principles:
- One container per task — no state bleed between benchmark instances.
- The host filesystem is never mounted — all file I/O goes through Docker's
  archive API (tar streams), same as the SWE-bench official harness.
- No network inside the container. The clone happens on the host, where network
  is allowed, and the result is streamed in.
- Memory and CPU are capped so a runaway process can't take the host with it.
- Every command carries a hard timeout. The agent runs test suites written by
  strangers; one of them will eventually hang.
"""

from __future__ import annotations

import contextlib
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
from sandbox.workspace import (
    CommandResult,
    CommandTimeout,
    SandboxError,
    _sh_quote,
    clone_repo,
    commit_baseline,
    read_diff,
)

__all__ = ["DockerWorkspace", "CommandResult", "CommandTimeout", "SandboxError"]

# ── Constants ─────────────────────────────────────────────────────────────────

SANDBOX_IMAGE = "swe-agent-sandbox:latest"
SANDBOX_DOCKERFILE = Path(__file__).parent / "Dockerfile.sandbox"
REPO_PATH_IN_CONTAINER = "/repo"
SANDBOX_USER = "sweagent"

DEFAULT_MEMORY = os.getenv("SANDBOX_MEMORY_LIMIT", "2g")
DEFAULT_CPU = float(os.getenv("SANDBOX_CPU_LIMIT", "2.0"))
DEFAULT_CMD_TIMEOUT = int(os.getenv("SANDBOX_CMD_TIMEOUT", "120"))

# GNU coreutils `timeout` exit code when it kills the child.
TIMEOUT_EXIT_CODE = 124

tracer = get_tracer(__name__)


# ── Docker workspace ──────────────────────────────────────────────────────────


@dataclass
class DockerWorkspace:
    """
    Isolated Docker container for a single SWE task.

    Usage:
        with DockerWorkspace.create(repo_url, commit_sha) as ws:
            result = ws.run("pytest tests/")
            content = ws.read_file("/repo/src/module.py")
            ws.write_file("/repo/src/module.py", new_content)
    """

    container: Container
    task_id: str
    repo_url: str
    commit_sha: str
    _client: docker.DockerClient = field(repr=False)
    _owner_ids: tuple[int, int] = (0, 0)

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
        Spin up a fresh sandbox container and check the repo out inside it.

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
                user=SANDBOX_USER,
                working_dir=REPO_PATH_IN_CONTAINER,
            )

            ws = cls(
                container=container,
                task_id=task_id,
                repo_url=repo_url,
                commit_sha=commit_sha,
                _client=client,
            )

            try:
                ws._setup_repo()
            except Exception:
                # A half-built workspace is worse than none: tear the container
                # down rather than leaking it on every failed task.
                ws.teardown()
                raise
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
        Get the repo at the right commit into the container.

        The container has no network, so the clone happens on the host and the
        result is streamed in as a tar. Ownership is then handed to the sandbox
        user: Docker's archive API writes as root, and a repo the agent cannot
        write to — or that git refuses to touch as "dubious ownership" — makes
        every later edit fail in a way that looks like a model mistake.
        """
        with tracer.start_as_current_span("sandbox.setup_repo"):
            with tempfile.TemporaryDirectory() as tmpdir:
                repo_dir = str(Path(tmpdir) / "repo")
                clone_repo(self.repo_url, self.commit_sha, repo_dir)
                self._copy_dir_to_container(repo_dir, REPO_PATH_IN_CONTAINER)

            self._exec_as_root(f"chown -R {SANDBOX_USER}:{SANDBOX_USER} {REPO_PATH_IN_CONTAINER}")
            self._owner_ids = self._read_owner_ids()

            # Install repo dependencies inside the container (best-effort — many
            # repos need no install step, and a failure here is not fatal).
            self.run("pip install -e . --quiet 2>/dev/null || true", timeout=300)

            commit_baseline(self)

    def _exec_as_root(self, command: str) -> None:
        """Run a setup command as root. Raises if it fails — setup must not be silent."""
        exit_code, output = self.container.exec_run(cmd=["bash", "-c", command], user="root")
        if exit_code != 0:
            detail = (output or b"").decode("utf-8", errors="replace")[:500]
            raise SandboxError(f"sandbox setup failed ({command!r}): {detail}")

    def _read_owner_ids(self) -> tuple[int, int]:
        """The sandbox user's numeric uid/gid, so files written in later stamp correctly."""
        result = self.run(f"id -u {SANDBOX_USER}; id -g {SANDBOX_USER}")
        try:
            uid, gid = (int(line) for line in result.stdout.split())
        except ValueError as exc:
            raise SandboxError(f"could not resolve {SANDBOX_USER} uid/gid: {result.output}") from exc
        return uid, gid

    def _copy_dir_to_container(self, host_path: str, container_path: str) -> None:
        """Copy a host directory into the container using Docker's archive API."""
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

        Each call is a fresh shell (not a persistent PTY) sharing the same
        filesystem, so `cd` does not carry across calls but edits do.

        The timeout is enforced by coreutils `timeout` inside the container
        rather than on the client: the Docker SDK's exec_run blocks with no
        deadline of its own, so a hanging test suite would otherwise hang the
        whole run. Exit code 124 is `timeout` reporting that it killed the child.
        """
        with tracer.start_as_current_span("sandbox.run") as span:
            span.set_attribute("command", command[:200])
            span.set_attribute("timeout", timeout)

            t0 = time.monotonic()

            try:
                exit_code, output = self.container.exec_run(
                    cmd=["timeout", "-k", "5", str(timeout), "bash", "-c", command],
                    workdir=workdir,
                    demux=True,
                    user=SANDBOX_USER,
                    environment={
                        "HOME": f"/home/{SANDBOX_USER}",
                        "PATH": "/usr/local/bin:/usr/bin:/bin",
                    },
                )
                stdout_bytes, stderr_bytes = output
                stdout = (stdout_bytes or b"").decode("utf-8", errors="replace")
                stderr = (stderr_bytes or b"").decode("utf-8", errors="replace")
            except Exception as exc:
                raise SandboxError(f"exec_run failed: {exc}") from exc

            duration_ms = int((time.monotonic() - t0) * 1000)
            timed_out = exit_code == TIMEOUT_EXIT_CODE

            result = CommandResult(
                command=command,
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                timed_out=timed_out,
                duration_ms=duration_ms,
                timeout_seconds=timeout,
            )

            span.set_attribute("exit_code", exit_code)
            span.set_attribute("duration_ms", duration_ms)
            span.set_attribute("timed_out", timed_out)
            return result

    def setup(self, command: str, timeout: int = 1800) -> CommandResult:
        """
        Run an environment-preparation command before the agent starts.

        Old commits routinely need dependency pins a modern interpreter would
        not choose on its own. Recorded alongside the run, because an
        environment nobody can reproduce makes the result unreproducible too.
        """
        print(f"[sandbox] setup: {command}")
        result = self.run(command, timeout=timeout)
        if not result.success:
            print(f"[sandbox] setup exited {result.exit_code}:\n{result.output[-2000:]}")
        return result

    # ── File I/O ──────────────────────────────────────────────────────────────

    def read_file(self, path: str) -> str:
        """Read a file from the container. Path is absolute inside the container."""
        result = self.run(f"cat {_sh_quote(path)}")
        if not result.success:
            raise FileNotFoundError(f"Cannot read {path} in sandbox: {result.output}")
        return result.stdout

    def write_file(self, path: str, content: str) -> None:
        """
        Write content to a file inside the container.

        Streamed in as a tar so the bytes land exactly as given — no shell
        quoting, no trailing-newline surprises — with the sandbox user's uid
        stamped on the member. Docker's archive API writes as root otherwise,
        and the agent would not be able to edit its own file a turn later.
        """
        uid, gid = self._owner_ids
        encoded = content.encode("utf-8")

        info = tarfile.TarInfo(name=Path(path).name)
        info.size = len(encoded)
        info.mode = 0o644
        info.uid, info.gid = uid, gid
        info.uname = info.gname = SANDBOX_USER

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            tar.addfile(info, io.BytesIO(encoded))
        buf.seek(0)
        self.container.put_archive(str(Path(path).parent), buf)

    def file_exists(self, path: str) -> bool:
        """Check if a file exists inside the container."""
        return self.run(f"test -f {_sh_quote(path)}").success

    def list_files(self, directory: str = REPO_PATH_IN_CONTAINER) -> list[str]:
        """List every file under a directory (absolute paths, .git excluded)."""
        result = self.run(f"find {_sh_quote(directory)} -type f -not -path '*/.git/*' | sort")
        if not result.success:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def get_diff(self) -> str:
        """Return the git diff of all changes the agent made to the repo."""
        return read_diff(self)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def teardown(self) -> None:
        """Stop and remove the container. Always call this when done."""
        with tracer.start_as_current_span("sandbox.teardown"):
            with contextlib.suppress(Exception):
                self.container.stop(timeout=10)
            with contextlib.suppress(Exception):
                self.container.remove(force=True)

    def __enter__(self) -> DockerWorkspace:
        return self

    def __exit__(self, *args) -> None:
        self.teardown()
