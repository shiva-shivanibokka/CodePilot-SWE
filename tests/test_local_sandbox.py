"""The host sandbox: timeouts that actually stop things, and a clean child env.

The first test is a reproduction. Before the fix, a 2-second timeout on a
command whose child left a grandchild holding stdout took 21.2 seconds on
Windows: `subprocess.run(timeout=...)` kills the shell it started, then blocks
reading a pipe the surviving grandchild still holds open. Autonomous-SWE-Agent's
local backend had already solved this with a process-group kill
(`B:sandbox/local_workspace.py::_run_with_deadline`), which is what was ported.
"""

from __future__ import annotations

import shutil
import sys
import time

import pytest

from codepilot.sandbox.local import LocalSandbox

GRANDCHILD = (
    f'"{sys.executable}" -c "import subprocess,sys,time; '
    "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
    'time.sleep(30)"'
)


async def test_a_timeout_kills_the_whole_process_tree(tmp_path):
    sandbox = LocalSandbox(root=tmp_path)
    started = time.monotonic()
    result = await sandbox.run(GRANDCHILD, timeout_seconds=2)
    elapsed = time.monotonic() - started
    assert result.timed_out
    assert elapsed < 12, f"a 2s timeout took {elapsed:.1f}s: the grandchild survived"


async def test_provider_keys_never_reach_the_child_when_scrubbed(tmp_path, monkeypatch):
    """Ported from B's test_provider_keys_are_stripped_from_the_child_environment.

    A command that dumps the environment must not be able to put a key into
    the transcript, the recording, or the model's context.
    """
    monkeypatch.setenv("GROQ_API_KEY", "gsk_should_not_leak")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_leak")
    monkeypatch.setenv("SOME_SECRET", "nope")
    monkeypatch.setenv("HARMLESS_SETTING", "fine")
    sandbox = LocalSandbox(root=tmp_path, scrub_secrets=True)
    result = await sandbox.run(
        f'"{sys.executable}" -c "import os; print(sorted(os.environ))"'
    )
    assert "GROQ_API_KEY" not in result.stdout
    assert "GITHUB_TOKEN" not in result.stdout
    assert "SOME_SECRET" not in result.stdout
    assert "HARMLESS_SETTING" in result.stdout


async def test_the_default_keeps_the_environment_for_the_interactive_tool(tmp_path, monkeypatch):
    """The CLI runs the user's own tests, which may legitimately need a token."""
    monkeypatch.setenv("MY_SERVICE_TOKEN", "x")
    result = await LocalSandbox(root=tmp_path).run(
        f'"{sys.executable}" -c "import os; print(\'MY_SERVICE_TOKEN\' in os.environ)"'
    )
    assert "True" in result.stdout


async def test_bare_python_resolves_to_the_configured_interpreter(tmp_path):
    """The benchmark points `python` at a per-task virtualenv, not at us."""
    sandbox = LocalSandbox(root=tmp_path, python=sys.executable)
    result = await sandbox.run('python -c "import sys; print(sys.executable)"')
    assert result.success, result.combined
    assert result.stdout.strip().lower() == sys.executable.lower()


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash on PATH")
async def test_a_posix_shell_can_be_required(tmp_path):
    """SWE-bench agents write POSIX shell; cmd.exe would fail them for nothing."""
    sandbox = LocalSandbox(root=tmp_path, shell=shutil.which("bash"))
    result = await sandbox.run("x=hello; echo ${x}-world | tr a-z A-Z")
    assert result.success, result.combined
    assert result.stdout.strip() == "HELLO-WORLD"
