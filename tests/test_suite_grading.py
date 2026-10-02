"""CodePilot's own task suite grades on a clean tree too.

Reproduction (docs/MERGE_DECISIONS.md, D16): the original runner wrote the
held-out test file into the agent's working tree and ran it there, so a
`conftest.py` the agent wrote rewrote the held-out result. With a scripted
model that only writes such a conftest and finishes, `run_one` reported
`passed=True` on the "empty-guard" task.
"""

from __future__ import annotations

from codepilot.bench.suite import runner
from codepilot.bench.suite.tasks import by_ids
from codepilot.llm import Reply, ToolCall, Usage
from tests.test_bench_grading import FORCE_PASS_CONFTEST


def _reply(name, args):
    return Reply(
        text="",
        content=[{"type": "tool_use", "id": "c", "name": name, "input": args}],
        tool_calls=[ToolCall(id="c", name=name, arguments=args)],
        stop_reason="tool_use", model="scripted",
        usage=Usage(input_tokens=10, output_tokens=10), latency_ms=1, cost_usd=0.0,
    )


class Cheater:
    def __init__(self, *a, **kw):
        self.model = "scripted"
        self.turn = 0

    async def chat(self, messages, **kw):
        self.turn += 1
        if self.turn == 1:
            return _reply("write_file", {"path": "tests/conftest.py", "content": FORCE_PASS_CONFTEST})
        return _reply("finish", {"summary": "done"})

    async def count_tokens(self, *a, **kw):
        return 10


async def test_an_agent_written_conftest_does_not_pass_a_suite_task(monkeypatch):
    monkeypatch.setattr(runner, "LLMClient", Cheater)
    (task,) = by_ids(["empty-guard"])
    result = await runner.run_one(task, "loop", model="scripted", effort=None, config_name="t")
    assert not result.passed, result.held_out_summary


FIXED = (
    "def mean(values):\n    if not values:\n        raise ValueError('empty')\n"
    "    return sum(values) / len(values)\n"
)


class Fixer(Cheater):
    async def chat(self, messages, **kw):
        self.turn += 1
        if self.turn == 1:
            return _reply("read_file", {"path": "stats.py"})
        if self.turn == 2:
            return _reply("write_file", {"path": "stats.py", "content": FIXED})
        return _reply("finish", {"summary": "guarded"})


async def test_a_real_fix_still_passes(monkeypatch):
    """Positive control: the clean-tree grading does not fail everything."""
    monkeypatch.setattr(runner, "LLMClient", Fixer)
    (task,) = by_ids(["empty-guard"])
    result = await runner.run_one(task, "loop", model="scripted", effort=None, config_name="t")
    assert result.passed, result.held_out_summary
