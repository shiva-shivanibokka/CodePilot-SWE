"""Spend controls added after the third review round (D41).

Each test names the hole it closes. None makes a real model call.
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import httpx
import litellm
import pytest

import codepilot.llm
from codepilot.llm import Ledger, LLMClient, ModelMismatch, SpendCapReached


def reply(model=None, prompt=1000, completion=10):
    return SimpleNamespace(
        model=model,
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None),
                                 finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
    )


@pytest.fixture
def fake(monkeypatch):
    sent: list[dict] = []
    script: list = []

    async def acompletion(**params):
        sent.append(params)
        await asyncio.sleep(0.05)  # let a concurrent caller interleave
        item = script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    monkeypatch.setattr(codepilot.llm.asyncio, "sleep", _fast_sleep)
    return sent, script


_real_sleep = asyncio.sleep


async def _fast_sleep(seconds):
    await _real_sleep(min(seconds, 0.01))


# ----------------------------------------------------- atomic reservation


async def test_two_clients_cannot_both_reserve_the_last_of_the_cap(fake, tmp_path):
    """Check-then-insert in separate steps let two callers both see room for
    one more call and both make it. Reservation is now one BEGIN IMMEDIATE
    transaction: exactly one of two concurrent calls fits under the cap."""
    sent, script = fake
    script += [reply(), reply()]
    ledger_file = tmp_path / "shared.sqlite"
    msgs = [{"role": "user", "content": "x" * 2000}]
    one = LLMClient(model="claude-haiku-4-5", ledger=Ledger(ledger_file), max_total_usd=0.01)
    worst = one.worst_case_usd("claude-haiku-4-5", one._request(
        msgs, system=None, tools=None, model="claude-haiku-4-5", max_tokens=1000,
        temperature=None, effort=None), 1000)
    cap = worst * 1.5  # room for one worst case, not two
    a = LLMClient(model="claude-haiku-4-5", ledger=Ledger(ledger_file), max_total_usd=cap)
    b = LLMClient(model="claude-haiku-4-5", ledger=Ledger(ledger_file), max_total_usd=cap)
    results = await asyncio.gather(
        a.chat(msgs, max_tokens=1000), b.chat(msgs, max_tokens=1000), return_exceptions=True)
    assert sum(isinstance(r, SpendCapReached) for r in results) == 1
    assert len(sent) == 1
    assert Ledger(ledger_file).total_usd() <= cap


# -------------------------------------------------------------- run lock


def test_a_second_paid_run_is_refused_and_a_stale_lock_can_be_recovered():
    from codepilot.bench import runlock

    with runlock.RunLock():
        with pytest.raises(runlock.RunLocked, match="break-stale-lock"):
            runlock.RunLock().acquire()
        with pytest.raises(runlock.RunLocked, match="still running"):
            runlock.break_stale_lock()  # our own pid holds it
    assert not runlock.lock_path().exists(), "released on exit"

    runlock.lock_path().write_text(json.dumps({"pid": 2_000_000_000, "started": "then"}))
    assert "removed stale lock" in runlock.break_stale_lock()
    assert not runlock.lock_path().exists()


def test_the_lock_is_released_on_ctrl_c():
    from codepilot.bench import runlock

    with pytest.raises(KeyboardInterrupt):
        with runlock.RunLock():
            raise KeyboardInterrupt
    assert not runlock.lock_path().exists()


def test_a_running_pid_is_seen_as_running():
    from codepilot.bench.runlock import pid_alive

    assert pid_alive(os.getpid())
    assert not pid_alive(2_000_000_000)


# ------------------------------------------------------------ hard maximum


@pytest.mark.parametrize("argv, expected", [
    (["--max-total-usd", "21"], "hard maximum"),
    ([], "needs --max-total-usd"),
])
def test_paid_runs_need_a_cap_no_higher_than_the_project_maximum(argv, expected, capsys):
    from codepilot.bench.run import PROJECT_MAX_USD, main

    assert PROJECT_MAX_USD == 20.0
    code = asyncio.run(main(["--sample", "1", "--arms", "agent", "--model", "claude-haiku-4-5", *argv]))
    assert code == 2
    assert expected in capsys.readouterr().out


# ---------------------------------------------------------- model mismatch


async def test_an_answer_from_another_model_is_recorded_and_aborts_the_run(fake, tmp_path):
    sent, script = fake
    script.append(reply(model="claude-sonnet-5-20260101"))
    ledger = Ledger(tmp_path / "l.sqlite")
    with pytest.raises(ModelMismatch, match="claude-sonnet-5"):
        await LLMClient(model="claude-haiku-4-5", ledger=ledger).chat(
            [{"role": "user", "content": "hi"}], max_tokens=10)
    (row,) = ledger.rows()
    assert row["status"] == "settled" and row["reported_model"] == "claude-sonnet-5-20260101"


async def test_a_dated_answer_to_an_alias_is_not_a_mismatch(fake, tmp_path):
    sent, script = fake
    script.append(reply(model="claude-haiku-4-5-20251001"))
    ledger = Ledger(tmp_path / "l.sqlite")
    await LLMClient(model="claude-haiku-4-5", ledger=ledger).chat(
        [{"role": "user", "content": "hi"}], max_tokens=10)
    assert ledger.rows()[0]["reported_model"] == "claude-haiku-4-5-20251001"


# ---------------------------------------------------------- transport errors


def _httpx2_read_timeout():
    import httpx2

    return httpx2.ReadTimeout("read timed out (httpx2)")


@pytest.mark.parametrize("make", [
    lambda: httpx.ReadTimeout("read timed out"),
    lambda: httpx.RemoteProtocolError("incomplete body"),
    lambda: httpx.ConnectError("refused"),
    _httpx2_read_timeout,
    lambda: litellm.Timeout("timed out", model="x", llm_provider="anthropic"),
    lambda: litellm.APIConnectionError("connection", model="x", llm_provider="anthropic"),
])
async def test_transport_errors_are_retried_once_and_every_attempt_is_charged(fake, tmp_path, make):
    sent, script = fake
    script += [make(), make()]
    ledger = Ledger(tmp_path / "l.sqlite")
    with pytest.raises(Exception):  # noqa: B017 - the transport error itself
        await LLMClient(model="claude-haiku-4-5", ledger=ledger).chat(
            [{"role": "user", "content": "hi"}], max_tokens=1000)
    assert len(sent) == 2
    rows = ledger.rows()
    assert [r["status"] for r in rows] == ["pending", "pending"]
    assert ledger.total_usd() == pytest.approx(sum(r["worst_usd"] for r in rows))
    assert ledger.total_usd() > 0


# ------------------------------------------------------ no unledgered client


def test_a_client_built_without_a_ledger_still_has_one():
    client = LLMClient(model="claude-haiku-4-5")
    assert client.ledger is not None
    assert client.ledger.path.parent == codepilot.llm.LEDGER_DIR


def test_no_code_path_constructs_a_client_with_ledger_none():
    """Grep the package: nothing may pass ledger=None explicitly."""
    from pathlib import Path

    root = Path(codepilot.llm.__file__).parent
    offenders = [p for p in root.rglob("*.py") if "ledger=None" in p.read_text(encoding="utf-8")]
    assert offenders == []


async def test_a_missing_docker_image_is_never_pulled_silently(monkeypatch):
    import docker

    from codepilot.bench import environment

    class Images:
        def get(self, name):
            raise docker.errors.ImageNotFound("absent")

    monkeypatch.setattr(docker, "from_env", lambda: SimpleNamespace(images=Images()))
    with pytest.raises(RuntimeError, match="docker pull"):
        environment._require_local_image("swebench/sweb.eval.x86_64.x_1776_y-1:latest")
