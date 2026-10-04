"""
One paid benchmark run at a time, per user (D41).

`bench.run` takes an exclusive lock file beside the user-level ledger for the
whole life of a run that can spend money, and refuses to start while another
holds it. The lock is created with O_EXCL, so two processes cannot both get it;
it records the holder's PID and command line; it is removed on normal exit, on
an exception and on Ctrl-C (a `finally`).

A lock left behind by a process that was killed outright is *stale*.
`python -m codepilot.bench.run --break-stale-lock` removes it, but only after
checking that the recorded PID is no longer running. This never kills
anything: it only looks the PID up (`tasklist` on Windows, `os.kill(pid, 0)`
on POSIX, where signal 0 tests existence without sending anything — on
Windows `os.kill` would terminate the process, so it is not used there).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


class RunLocked(RuntimeError):
    """Another paid run holds the lock."""


def lock_path() -> Path:
    from codepilot import llm

    return llm.LEDGER_DIR / "run.lock"


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, check=False,
        ).stdout
        return f'"{pid}"' in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def holder() -> dict | None:
    path = lock_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None if not path.exists() else {"pid": -1, "unreadable": True}


class RunLock:
    def __init__(self) -> None:
        self.path = lock_path()
        self.held = False

    def acquire(self) -> RunLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            info = holder() or {}
            raise RunLocked(
                f"another paid run holds {self.path} (pid {info.get('pid')}, started "
                f"{info.get('started')}). If that process is gone, run "
                "`python -m codepilot.bench.run --break-stale-lock`."
            ) from None
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
                       "argv": sys.argv}, fh)
        self.held = True
        return self

    def release(self) -> None:
        if self.held:
            self.path.unlink(missing_ok=True)
            self.held = False

    def __enter__(self) -> RunLock:
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


def break_stale_lock() -> str:
    """Remove the lock only if its holder is not running. Kills nothing."""
    path = lock_path()
    info = holder()
    if info is None:
        return f"no lock at {path}"
    pid = int(info.get("pid") or -1)
    if pid_alive(pid):
        raise RunLocked(f"pid {pid} is still running; the lock at {path} is not stale")
    path.unlink(missing_ok=True)
    return f"removed stale lock {path} (pid {pid} is not running)"
