"""End to end, offline: a scripted model drives CodePilot's loop through real
tools, and both arms go through the real benchmark harness — checkout, shared
environment, selection, clean-tree grading — on a local SWE-bench-shaped task.

Nothing is mocked below the model. The "model" is a script that answers by
phase (the arm's system prompt says which phase it is in) and, for the agent,
by how many turns it has already taken.
"""

from __future__ import annotations

import json

import pytest

from codepilot.bench.harness import ArmConfig, run_instance
from codepilot.llm import Reply, ToolCall, Usage
from tests.benchfixture import make_task
from tests.test_bench_grading import FORCE_PASS_CONFTEST

FIX = {"search": "    return a - b", "replace": "    return a + b"}
BREAKS_DOUBLE = {"search": "    return x * 2", "replace": "    return x * 3"}


def reply(text="", calls=()):
    return Reply(
        text=text,
        content=([{"type": "text", "text": text}] if text else [])
        + [{"type": "tool_use", "id": f"c{i}", "name": n, "input": a} for i, (n, a) in enumerate(calls)],
        tool_calls=[ToolCall(id=f"c{i}", name=n, arguments=a) for i, (n, a) in enumerate(calls)],
        stop_reason="tool_use" if calls else "end_turn",
        model="scripted",
        usage=Usage(input_tokens=1000, output_tokens=100, cache_read_tokens=500),
        latency_ms=1,
        cost_usd=0.001,
    )


AGENT_FIXES = [
    [("list_files", {})],
    [("search_code", {"query": "add subtracts"})],
    [("read_file", {"path": "calc.py"})],
    [("edit_file", {"path": "calc.py", "old": "return a - b", "new": "return a + b"})],
    [("run_tests", {"command": "python -m pytest tests -q"})],
    [("finish", {"summary": "add() subtracted; it now adds."})],
]

AGENT_CHEATS = [
    [("write_file", {"path": "tests/conftest.py", "content": FORCE_PASS_CONFTEST})],
    [("finish", {"summary": "tests pass now"})],
]


class ScriptedModel:
    def __init__(self, agent_scripts, samples):
        self.agent_scripts = list(agent_scripts)  # one script per attempt
        self.samples = list(samples)
        self.calls = []
        self.model = "scripted"

    async def count_tokens(self, messages, **kw):
        return 100

    async def chat(self, messages, system=None, tools=None, temperature=None, **kw):
        text = system[0]["text"] if isinstance(system, list) else str(system)
        self.calls.append((text.split("## ", 1)[-1][:20], temperature))
        if "localise" in text:
            return reply(json.dumps({
                "suspect_files": ["calc.py"],
                "suspect_locations": [{"file": "calc.py", "function_name": "add", "class_name": None}],
            }))
        if "repair" in text:
            return reply(json.dumps({"explanation": "x", **self.samples.pop(0)}))
        # The agent: a new attempt starts with a single user message.
        if len(messages) == 1:
            self.current = self.agent_scripts.pop(0)
        turn = sum(1 for m in messages if m["role"] == "assistant")
        return reply(calls=self.current[min(turn, len(self.current) - 1)])


@pytest.fixture
def task(tmp_path):
    instance, _ = make_task(tmp_path)
    return instance


async def run(task, model, arms, attempts):
    return await run_instance(
        task, arms, ArmConfig(model="scripted", attempts=attempts, max_turns=20),
        client=model, env_options={"install": False, "venv": False},
    )


async def test_both_arms_resolve_through_the_real_harness(task):
    model = ScriptedModel([AGENT_FIXES], [FIX])
    agent, agentless = await run(task, model, ["agent", "agentless"], attempts=1)

    assert agent.resolved, agent.grade
    assert agent.model_calls == len(AGENT_FIXES)
    assert agent.stopped_by == ["finished"]
    assert agent.changed_lines == 2
    assert agent.cost_usd == pytest.approx(0.001 * len(AGENT_FIXES))
    assert agent.cache_read_tokens == 500 * len(AGENT_FIXES)
    assert "calc.py" in agent.diff and "tests/" not in agent.diff

    assert agentless.resolved, agentless.grade
    assert agentless.model_calls == 2  # one localisation, one sample
    assert agentless.selection_basis.startswith("single attempt")


async def test_every_arm_gets_the_same_system_prompt_base(task):
    from codepilot.bench.prompts import SHARED_BASE

    seen = []

    class Recording(ScriptedModel):
        async def chat(self, messages, system=None, **kw):
            seen.append(system)
            return await super().chat(messages, system=system, **kw)

    await run(task, Recording([AGENT_FIXES], [FIX]), ["agent", "agentless"], attempts=1)
    for blocks in seen:
        assert len(blocks) == 1, "one system block, one breakpoint"
        assert blocks[0]["text"].startswith(SHARED_BASE)
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}


async def test_budget_matched_selection_is_not_first_that_breaks_nothing(task):
    """Three samples: the first breaks an existing test, the other two fix the
    bug. Every sample is evaluated, the breaking one is caught test by test,
    and the two identical fixes win the vote. Autonomous-SWE-Agent's rule
    validated in order and stopped at the first candidate it judged harmless."""
    model = ScriptedModel([], [BREAKS_DOUBLE, FIX, FIX])
    (agentless,) = await run(task, model, ["agentless"], attempts=3)
    assert agentless.resolved, (agentless.selection_basis, agentless.candidates)
    assert agentless.votes == 2
    assert "majority vote among regression-free" in agentless.selection_basis
    regress = {c["origin"]: c["n_regressions"] for c in agentless.candidates}
    assert regress[next(o for o in regress if "sample 1" in o)] == 1
    # The schedule both arms share: near-greedy first, then diverse.
    temps = [t for phase, t in model.calls if phase.startswith("Your job: repair")]
    assert temps == [0.2, 1.0, 1.0]


async def test_an_agent_that_games_the_tests_with_conftest_is_not_resolved(task):
    model = ScriptedModel([AGENT_CHEATS], [])
    (agent,) = await run(task, model, ["agent"], attempts=1)
    assert not agent.resolved
    assert "tests/conftest.py" in agent.grade["dropped"]


async def test_agent_attempts_go_through_the_same_selection(task):
    model = ScriptedModel([AGENT_CHEATS, AGENT_FIXES], [])
    (agent,) = await run(task, model, ["agent"], attempts=2)
    assert agent.resolved
    assert agent.selected == "agent attempt 2"
    assert agent.stopped_by == ["finished", "finished"]
    assert [c["applied"] for c in agent.candidates] == [False, True]


# ------------------------------------------------------------------ seeds
#
# Ported from the parallel SOP eval branch of Autonomous-SWE-Agent (commit
# 9911b2d, tests/test_repair_seeds.py): with one seed sent for every sample,
# the same prompt at temperature 1 is the same sample, so "N candidates"
# silently becomes one. Each sample and each agent attempt gets its own seed.


class SeedRecorder(ScriptedModel):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.seeds = []

    async def chat(self, messages, system=None, seed=None, **kw):
        self.seeds.append(seed)
        return await super().chat(messages, system=system, **kw)


async def test_a_seeded_run_gives_every_sample_and_attempt_its_own_seed(task):
    model = SeedRecorder([AGENT_FIXES, AGENT_FIXES], [FIX, FIX, FIX])
    await run_instance(
        task, ["agentless", "agent"],
        ArmConfig(model="scripted", attempts=2, max_turns=20, seed=7),
        client=model, env_options={"install": False, "venv": False},
    )
    sample_seeds = model.seeds[1:3]          # after the localisation call
    agent_seeds = set(model.seeds[3:])
    assert len(set(sample_seeds)) == 2, sample_seeds
    assert agent_seeds == {7000, 7001}, agent_seeds
    assert not set(sample_seeds) & agent_seeds


async def test_an_unseeded_run_sends_no_seed(task):
    model = SeedRecorder([AGENT_FIXES], [FIX])
    await run(task, model, ["agentless", "agent"], attempts=1)
    assert set(model.seeds) == {None}


async def test_a_result_says_what_the_agent_did(task):
    """Found by the first local-model smoke run: an agent row read
    stopped_by=["finished"] after 1 call and nothing recorded why. Every row
    now carries a compact transcript: the model's words and each tool call."""
    model = ScriptedModel([AGENT_FIXES], [FIX])
    agent, agentless = await run(task, model, ["agent", "agentless"], attempts=1)
    tools = [t["tool"] for t in agent.transcript if t["kind"] == "tool_call"]
    assert tools == [step[0][0] for step in AGENT_FIXES]
    assert any(t["kind"] == "done" and "add() subtracted" in t["text"] for t in agent.transcript)
    assert agentless.transcript, "the agentless arm records its samples too"


async def test_agentless_spend_survives_a_later_stage_failing(task, monkeypatch, tmp_path):
    """Reproduction (D27): agentless added its spend to the row only after
    run_agentless returned, so a repair stage that raised lost the
    localisation's cost from the row — money spent and not reported."""
    from types import SimpleNamespace

    import litellm

    from codepilot.llm import LLMClient

    calls = []

    async def fake(**params):
        calls.append(params)
        if len(calls) == 1:
            return SimpleNamespace(
                model="claude-haiku-4-5",
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                    "suspect_files": ["calc.py"],
                    "suspect_locations": [{"file": "calc.py", "function_name": "add"}]}),
                    tool_calls=None), finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=2000, completion_tokens=50),
            )
        raise RuntimeError("the repair stage fell over")

    monkeypatch.setattr(litellm, "acompletion", fake)
    client = LLMClient(model="claude-haiku-4-5")
    (row,) = await run_instance(
        task, ["agentless"], ArmConfig(model="claude-haiku-4-5", attempts=1),
        client=client, env_options={"install": False, "venv": False},
    )
    assert "fell over" in row.error
    assert row.model_calls == 1
    assert row.cost_usd == pytest.approx(2000 * 1e-6 + 50 * 5e-6)


# ---------------------------------------------------------------- spend cap


def _fake_reply(text="", tool=None, prompt=3000, completion=200):
    from types import SimpleNamespace

    calls = None
    if tool:
        calls = [SimpleNamespace(id="c1", function=SimpleNamespace(name=tool[0], arguments=json.dumps(tool[1])))]
    return SimpleNamespace(
        model="claude-haiku-4-5",
        choices=[SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=calls),
                                 finish_reason="tool_calls" if tool else "stop")],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
    )


async def test_the_spend_cap_stops_the_whole_run_across_both_arms(task, monkeypatch, tmp_path):
    """Reproduction (D28): there was no run-wide cap — only a per-attempt
    budget, which never saw agentless calls at all. With a $0.02 cap, the run
    must stop before the call that could cross it, whichever arm makes it, and
    the stop must not be scored as an agent failure."""
    import litellm

    from codepilot.llm import Ledger, LLMClient, SpendCapReached

    sent = []

    async def fake(**params):
        sent.append(params)
        return _fake_reply(tool=("list_files", {}))

    monkeypatch.setattr(litellm, "acompletion", fake)
    ledger = Ledger(tmp_path / "ledger.jsonl")
    client = LLMClient(model="claude-haiku-4-5", ledger=ledger, max_total_usd=0.02)
    with pytest.raises(SpendCapReached):
        await run_instance(
            task, ["agent", "agentless"],
            ArmConfig(model="claude-haiku-4-5", attempts=1, max_turns=50, max_output_tokens=512),
            client=client, env_options={"install": False, "venv": False},
        )
    assert ledger.total_usd() <= 0.02
    assert len(sent) == len(ledger.rows()) >= 1


async def test_a_new_run_counts_what_the_ledger_already_holds(tmp_path, monkeypatch):
    import litellm

    from codepilot.llm import Ledger, LLMClient, SpendCapReached

    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.append({"tag": "earlier", "cost_usd": 0.019})

    async def fake(**params):
        return _fake_reply(text="hi")

    monkeypatch.setattr(litellm, "acompletion", fake)
    client = LLMClient(model="claude-haiku-4-5", ledger=ledger, max_total_usd=0.02)
    with pytest.raises(SpendCapReached, match=r"\$0\.0190 spent"):
        await client.chat([{"role": "user", "content": "x" * 4000}], max_tokens=512)


async def test_an_unpriced_model_cannot_run_under_a_cap(monkeypatch):
    from codepilot.llm import LLMClient, SpendCapReached

    client = LLMClient(model="nobody/unknown-model", max_total_usd=1.0)
    with pytest.raises(SpendCapReached, match="no price"):
        await client.chat([{"role": "user", "content": "hi"}], max_tokens=10)


# ------------------------------------------- shared environment (D35)
#
# Reproduction: both arms run in one environment, restored to the baseline
# between them — but `restore` only puts *tracked* files back. Ignored files
# that setup created (build output, egg-info) are kept as they are, and the
# installed packages are never checked, so an arm that changed either left the
# next arm, and its own grading, running in a different environment, silently.

CONTAMINATE = [
    [("run_command", {"command": "echo tampered > build/artifact.txt"})],
    [("finish", {"summary": "done"})],
]


async def test_an_arm_that_changes_the_kept_environment_is_flagged(tmp_path):
    instance, _ = make_task(tmp_path)
    instance = dict(instance)
    model = ScriptedModel([CONTAMINATE, AGENT_FIXES], [])
    rows = await run_instance(
        instance, ["agent", "agent"], ArmConfig(model="scripted", attempts=1, max_turns=20),
        client=model, setup="mkdir -p build && echo original > build/artifact.txt",
        env_options={"install": False, "venv": False},
    )
    assert rows[0].contamination, "the first arm rewrote a kept file"
    assert any("build/artifact.txt" in c for c in rows[0].contamination)
    assert rows[1].contamination, "the second arm started in the changed environment"


async def test_arm_order_is_randomised_per_instance_and_recorded(task):
    orders = set()
    for seed in range(6):
        model = ScriptedModel([AGENT_FIXES], [FIX])
        rows = await run_instance(
            task, ["agent", "agentless"], ArmConfig(model="scripted", attempts=1, max_turns=20, seed=seed),
            client=model, env_options={"install": False, "venv": False},
        )
        orders.add(tuple(r.arm for r in rows))
        assert all(r.arm_order == [x.arm for x in rows] for r in rows)
        if len(orders) == 2:
            break
    assert orders == {("agent", "agentless"), ("agentless", "agent")}


async def test_the_fingerprint_really_sees_the_installed_packages(tmp_path):
    from codepilot.bench.environment import BenchEnv

    instance, _ = make_task(tmp_path)
    env = await BenchEnv.create(instance["repo_url"], instance["base_commit"], install=False,
                                venv=False, task_id="fp")
    try:
        prints = await env.fingerprint()
        site = [k for k in prints if k.startswith("site:")]
        assert len(site) > 100 and not any("<" in k for k in site), site[:3]
        assert await env.fingerprint() == prints, "nothing changed, nothing flagged"
    finally:
        await env.close()
