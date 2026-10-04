"""The cap must be a real number, and the ledger must not move (D45).

Both holes were found by a live probe of the spend path, not by inference:

* `--max-total-usd nan` passed every gate, because every comparison against
  `nan` is False. `inf` was already refused (it fails `> PROJECT_MAX_USD`),
  0 and negative were harmless (every reservation exceeds them).
* Pointing `LOCALAPPDATA` elsewhere gave a fresh $0 ledger *and* moved the
  lock, so the canary's spend was forgotten and two paid runs could overlap.
"""

from __future__ import annotations

import asyncio
import importlib
import math
from types import SimpleNamespace

import pytest

import codepilot.llm
from codepilot.bench import run as bench_run
from codepilot.bench import runlock
from codepilot.llm import Ledger, SpendCapReached

NOT_A_CAP = [math.nan, math.inf, -math.inf, 0.0, -1.0]


# --------------------------------------------------------------- S1, layer 1

def _argv(cap: str) -> list[str]:
    """The gate is checked before anything runs; --dry-run keeps it that way.

    Without it, `nan` let a *real* run start — the probe that found this bug.
    """
    return ["--instances", "pallets__flask-5063", "--arms", "agent",
            "--model", "claude-haiku-4-5-20251001", f"--max-total-usd={cap}", "--dry-run"]


@pytest.mark.parametrize("cap", ["nan", "NaN", "inf", "-inf", "0", "-1"])
def test_a_cap_that_is_not_a_positive_number_is_refused(cap, capsys):
    assert asyncio.run(bench_run.main(_argv(cap))) == 2
    out = capsys.readouterr().out
    # Refused by the paid gate, not by the dry run's own comparison.
    assert "--max-total-usd" in out and "worst case" not in out


@pytest.mark.parametrize("cap", NOT_A_CAP)
def test_the_dry_run_refuses_a_cap_it_cannot_compare_against(cap, capsys):
    """run.py:129's `worst > cap` is False for nan: guard it independently."""
    args = SimpleNamespace(
        model="claude-haiku-4-5-20251001", instances=["pallets__flask-5063"], sample=None,
        attempts=1, max_cost=0.40, max_prompt_tokens=50_000, max_output_tokens=2048,
        max_total_usd=cap,
    )
    assert bench_run.dry_run(args, ["agent"]) == 2
    assert "--max-total-usd" in capsys.readouterr().out


# --------------------------------------------------------------- S1, layer 2

@pytest.mark.parametrize("cap", NOT_A_CAP)
def test_the_ledger_fails_closed_on_a_cap_that_is_not_a_positive_number(cap, tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite")
    with pytest.raises(SpendCapReached):
        ledger.reserve("c1", tag="t", model="m", worst=0.01, cap=cap)
    assert ledger.total_usd() == 0.0


def test_a_real_cap_still_reserves(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite")
    ledger.reserve("c1", tag="t", model="m", worst=0.01, cap=20.0)
    assert ledger.total_usd() == pytest.approx(0.01)


# ---------------------------------------------------------------------- S2

def test_the_ledger_and_the_lock_do_not_follow_localappdata(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "elsewhere"))
    reloaded = importlib.reload(codepilot.llm)
    try:
        moved = reloaded.LEDGER_DIR
    finally:
        importlib.reload(codepilot.llm)
    assert tmp_path not in moved.parents
    assert moved == codepilot.llm._ledger_home()


def test_the_ledger_home_is_the_users_home_directory():
    from pathlib import Path

    assert Path.home() in codepilot.llm._ledger_home().parents


def test_the_lock_lives_beside_the_ledger():
    assert runlock.lock_path().parent == codepilot.llm.LEDGER_DIR
