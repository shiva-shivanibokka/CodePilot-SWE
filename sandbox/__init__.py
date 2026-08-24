"""
Workspace backends and the factory that chooses between them.

Set WORKSPACE_BACKEND to pick one explicitly:

    docker  (default) — one throwaway, network-less container per task.
    local             — a temp directory on this machine, no isolation.

`docker` is the right answer whenever the repo under test is untrusted, which on
a benchmark of 300 arbitrary GitHub projects is always. `local` exists so the
agent can still be run — and recorded — on a machine without Docker.
"""

from __future__ import annotations

import os

from sandbox.workspace import CommandResult, CommandTimeout, SandboxError, Workspace

__all__ = [
    "CommandResult",
    "CommandTimeout",
    "SandboxError",
    "Workspace",
    "create_workspace",
    "resolve_backend",
]

DEFAULT_BACKEND = "docker"


def resolve_backend(backend: str | None = None) -> str:
    """Normalise and validate a backend name."""
    name = (backend or os.getenv("WORKSPACE_BACKEND") or DEFAULT_BACKEND).strip().lower()
    if name not in ("docker", "local"):
        raise ValueError(f"Unknown workspace backend {name!r}. Options: docker, local")
    return name


def create_workspace(
    repo_url: str,
    commit_sha: str,
    task_id: str | None = None,
    backend: str | None = None,
) -> Workspace:
    """
    Build a workspace with the repo checked out at `commit_sha`.

    Both backends are imported lazily: the Docker SDK and the local backend each
    pull dependencies the other does not need, and importing this package should
    not require both to be present.
    """
    name = resolve_backend(backend)

    if name == "local":
        from sandbox.local_workspace import LocalWorkspace

        return LocalWorkspace.create(repo_url, commit_sha, task_id=task_id)

    from sandbox.docker_workspace import DockerWorkspace

    return DockerWorkspace.create(repo_url, commit_sha, task_id=task_id)
