"""The commands in bench/STUDY_PLAN.md's runbook, run exactly as written (D43).

Each fenced block after a `<!-- runbook:NAME -->` marker is parsed and handed
to `codepilot.bench.run.main`. Environments and grading are replaced by fakes,
but every model request goes through the real LLMClient, ledger and lock, with
`litellm.acompletion` faked: the documented commands must parse, respect the
project maximum, take and release the lock, and land in the ledger.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
from pathlib import Path
from types import SimpleNamespace

import litellm
import pytest

import codepilot.llm
from codepilot.bench import run as bench_run
from codepilot.bench import runlock
from codepilot.llm import Ledger

PLAN = Path(__file__).resolve().parents[1] / "bench" / "STUDY_PLAN.md"
MODEL = "claude-haiku-4-5-20251001"


def runbook() -> dict[str, list[list[str]]]:
    text = PLAN.read_text(encoding="utf-8")
    blocks = re.findall(r"<!-- runbook:([\w-]+) -->\s*```bash\n(.*?)```", text, re.S)
    out: dict[str, list[list[str]]] = {}
    for name, body in blocks:
        cmds = [shlex.split(line) for line in body.splitlines() if line.strip()]
        out[name] = cmds
    return out


def argv_of(cmd: list[str]) -> list[str]:
    assert cmd[:3] == ["python", "-m", "codepilot.bench.run"], cmd
    return cmd[3:]


def test_the_plan_documents_every_runbook_step():
    book = runbook()
    assert set(book) >= {"harness-check", "dry-run", "canary", "main"}
    assert len(book["canary"]) == len(book["main"]) == 1


def _reply(prompt=3000, completion=200):
    return SimpleNamespace(
        model=MODEL,
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None),
                                 finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
    )


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    sent: list[dict] = []
    seen_lock: list[bool] = []

    async def acompletion(**params):
        sent.append(params)
        return _reply()

    async def run_instance(inst, arms, cfg, *, client, on_result, **_):
        seen_lock.append(runlock.lock_path().exists())
        for arm in arms:
            await client.chat([{"role": "user", "content": f"fix {inst['instance_id']}"}],
                              max_tokens=cfg.max_output_tokens,
                              cache_tag=f"{inst['instance_id']}:{arm}:0")
            row = {"instance_id": inst["instance_id"], "arm": arm, "resolved": False}
            on_result(SimpleNamespace(to_dict=lambda row=row: row, arm=arm, resolved=False,
                                      cost_usd=0.0, model_calls=1, wall_seconds=0.0,
                                      infra_error=None, error=None))

    async def check_harness(inst, arms, **_):
        return [{"instance_id": inst["instance_id"], "arm": a, "resolved": a == "gold",
                 "ok": True} for a in arms]

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    monkeypatch.setattr(bench_run, "run_instance", run_instance)
    monkeypatch.setattr(bench_run, "check_harness", check_harness)
    return sent, seen_lock


def _rows(path: str) -> list[dict]:
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x]


def _out(argv: list[str]) -> str:
    return argv[argv.index("--out") + 1]


def test_harness_check_command_runs_without_a_model(fakes):
    sent, seen_lock = fakes
    (cmd,) = runbook()["harness-check"]
    argv = argv_of(cmd)
    assert asyncio.run(bench_run.main(argv)) == 0
    rows = _rows(_out(argv))
    assert len(rows) == 40 and {r["arm"] for r in rows} == {"gold", "empty"}
    assert sent == [] and not runlock.lock_path().exists()


def test_dry_run_commands_fit_under_their_caps(fakes, capsys):
    sent, _ = fakes
    for cmd in runbook()["dry-run"]:
        argv = argv_of(cmd)
        assert "--dry-run" in argv and MODEL in argv
        assert asyncio.run(bench_run.main(argv)) == 0, capsys.readouterr().out
    assert sent == []
    assert "worst case total" in capsys.readouterr().out


def test_canary_then_main_run_as_documented(fakes):
    sent, seen_lock = fakes
    book = runbook()
    canary, main = argv_of(book["canary"][0]), argv_of(book["main"][0])
    for argv, cap in ((canary, 1.0), (main, bench_run.PROJECT_MAX_USD)):
        assert float(argv[argv.index("--max-total-usd") + 1]) == cap
        assert argv[argv.index("--model") + 1] == MODEL

    assert asyncio.run(bench_run.main(canary)) == 0
    assert asyncio.run(bench_run.main(main)) == 0

    # The lock was held while the model ran, and released after.
    assert seen_lock and all(seen_lock)
    assert not runlock.lock_path().exists()
    # 1 + 20 instances x 2 arms; the canary's instance came from the response cache.
    ledger = Ledger.default()
    assert ledger.path.parent == codepilot.llm.LEDGER_DIR
    assert len(sent) == 40
    total = ledger.total_usd()
    assert 0 < total <= bench_run.PROJECT_MAX_USD
    for argv in (canary, main):
        rows = _rows(_out(argv))
        config = rows[0]
        assert config["arm"] == "config" and config["model"] == MODEL
        assert config["max_total_usd"] == float(argv[argv.index("--max-total-usd") + 1])


def test_main_run_stops_at_its_cap(fakes, monkeypatch):
    """With the ledger already near the cap, the documented main run aborts."""
    sent, _ = fakes
    main = argv_of(runbook()["main"][0])
    Ledger.default().append({"tag": "earlier", "model": MODEL, "cost_usd": 19.999})
    assert asyncio.run(bench_run.main(main)) == 3
    assert sent == []
    rows = _rows(_out(main))
    assert rows[-1]["arm"] == "aborted"
    assert not runlock.lock_path().exists()


def test_the_runbook_leaves_the_agent_room_to_compact():
    """C1/D45: compaction fires above --compact-at, but a prompt over
    --max-prompt-tokens is refused first, so a default --compact-at of 100k
    with a 50k prompt bound killed every agent attempt that grew past 50k."""
    for name in ("canary", "main"):
        argv = argv_of(runbook()[name][0])
        compact_at = int(argv[argv.index("--compact-at") + 1])
        bound = int(argv[argv.index("--max-prompt-tokens") + 1])
        assert compact_at < bound, f"{name}: compaction can never run"


def test_the_runbook_gives_both_arms_the_same_output_budget():
    """C2/D45: 2,048 for the agent against a hardcoded 4,096 for agentless."""
    from codepilot.bench.agentless.repair import SAMPLE_MAX_TOKENS

    for name in ("canary", "main", "dry-run"):
        for cmd in runbook()[name]:
            argv = argv_of(cmd)
            assert int(argv[argv.index("--max-output-tokens") + 1]) == SAMPLE_MAX_TOKENS
