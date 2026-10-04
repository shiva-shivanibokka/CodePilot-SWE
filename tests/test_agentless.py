"""The agentless pieces that do not need a model, ported from
Autonomous-SWE-Agent's tests/test_regressions.py and rewritten against the
merged code. Each class says what it guards."""

from __future__ import annotations

import pytest

from codepilot.bench.agentless.jsonx import extract_json
from codepilot.bench.agentless.repair import apply_search_replace, temperature_for
from codepilot.bench.selection import nearest_test_dirs, regressions_between


class TestJsonExtraction:
    """Models wrap JSON in prose and fences, and the JSON may itself contain a
    brace or a fence. Regex-then-json.loads breaks on exactly those cases."""

    def test_plain_object(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_with_prose(self):
        text = 'Sure, here you go:\n```json\n{"a": 1}\n```\nHope that helps.'
        assert extract_json(text) == {"a": 1}

    def test_braces_inside_strings(self):
        text = '```\n{"search": "if x: {y}", "replace": "if x: {z}"}\n```'
        assert extract_json(text, expect=dict)["search"] == "if x: {y}"

    def test_expect_type_skips_a_leading_array(self):
        assert extract_json('[1, 2] then {"a": 1}', expect=dict) == {"a": 1}

    def test_nothing_parseable_raises(self):
        with pytest.raises(ValueError):
            extract_json("no json at all here")


class TestSearchReplacePatching:
    """Every rejected sample carries a reason: a phase that yields no patch must
    say whether the model was truncated, hallucinated, or was ambiguous."""

    FILE = "def f(a):\n    return a\n\n\ndef g(b):\n    return b\n"

    def test_applies_a_unique_match(self):
        patched, explanation = apply_search_replace(
            self.FILE, '{"explanation": "fix g", "search": "return b", "replace": "return b + 1"}'
        )
        assert patched == "def f(a):\n    return a\n\n\ndef g(b):\n    return b + 1\n"
        assert explanation == "fix g"

    def test_truncated_response_is_reported_not_dropped(self):
        patched, reason = apply_search_replace(self.FILE, '{"search": "ret', "max_tokens")
        assert patched is None and "token cap" in reason

    def test_a_complete_reply_is_not_treated_as_truncated(self):
        patched, reason = apply_search_replace("x = 1\n", "", "end_turn")
        assert patched is None and "token cap" not in reason

    def test_hallucinated_search_is_rejected(self):
        patched, reason = apply_search_replace(self.FILE, '{"search": "return zzz", "replace": "x"}')
        assert patched is None and "does not appear" in reason

    def test_ambiguous_search_is_rejected(self):
        patched, reason = apply_search_replace("x = 1\nx = 1\n", '{"search": "x = 1", "replace": "x = 2"}')
        assert patched is None and "matches 2" in reason

    def test_noop_patch_is_rejected(self):
        patched, reason = apply_search_replace(self.FILE, '{"search": "return b", "replace": "return b"}')
        assert patched is None and "changes nothing" in reason


class TestRegressionsByTestId:
    """B's validation-baseline scenarios, now judged test by test.

    B compared (passed, failed, errors) counts against a baseline. The cases it
    guarded still hold; the count rule's blind spot (a patch that fixes one
    test and breaks another nets to zero) does not.
    """

    BASE = {"t::a": "PASSED", "t::b": "PASSED", "t::old": "FAILED"}

    def test_pre_existing_failures_do_not_reject_a_good_patch(self):
        assert regressions_between(self.BASE, dict(self.BASE)) == []

    def test_a_patch_that_breaks_something_is_rejected(self):
        assert regressions_between(self.BASE, {**self.BASE, "t::b": "FAILED"}) == ["t::b"]

    def test_a_patch_that_fixes_something_is_kept(self):
        assert regressions_between(self.BASE, {**self.BASE, "t::old": "PASSED"}) == []

    def test_a_test_that_stops_being_collected_is_a_regression(self):
        after = {"t::a": "PASSED", "t::old": "FAILED"}
        assert regressions_between(self.BASE, after) == ["t::b"]

    def test_new_errors_are_rejected(self):
        assert regressions_between(self.BASE, {**self.BASE, "t::a": "ERROR"}) == ["t::a"]

    def test_a_swap_that_nets_to_zero_is_still_caught(self):
        """Fix t::old, break t::a: counts are unchanged, which B's rule accepted."""
        after = {"t::a": "FAILED", "t::b": "PASSED", "t::old": "PASSED"}
        assert regressions_between(self.BASE, after) == ["t::a"]


def test_the_tests_nearest_a_changed_file_are_chosen():
    files = [
        "sympy/physics/units/quantities.py",
        "sympy/physics/units/tests/test_quantities.py",
        "sympy/core/tests/test_basic.py",
        "src/flask/config.py",
        "tests/test_config.py",
    ]
    assert nearest_test_dirs(["sympy/physics/units/quantities.py"], files) == [
        "sympy/physics/units/tests"
    ]
    assert nearest_test_dirs(["src/flask/config.py"], files) == ["tests"]


def test_the_sampling_schedule_is_greedy_then_diverse():
    assert [temperature_for(i) for i in range(3)] == [0.2, 1.0, 1.0]


def test_the_cost_estimate_scales_attempts_and_calls_as_documented():
    from codepilot.bench.estimate import plan_tokens

    measured = {
        "agent": {"in_per_unit": 1000, "out_per_unit": 10},
        "agentless": {"in_per_unit": 100, "out_per_unit": 1},
    }
    t = plan_tokens(measured, instances=50, seeds=3, attempts=3)
    assert t["agent"] == (150 * 3 * 1000, 150 * 3 * 10)
    assert t["agentless"] == (150 * 4 * 100, 150 * 4 * 1)  # 1 localisation + 3 samples


async def test_a_truncated_sample_is_asked_again_with_twice_the_room(tmp_path):
    """Restores B's TestTruncatedSampleRetry for the merged repair(): a reply
    cut off at the token cap is re-asked once at double the cap, and the
    re-asked reply is the one used."""
    import json

    from codepilot.bench.agentless.localize import LocalizationResult
    from codepilot.bench.agentless.repair import SAMPLE_MAX_TOKENS, repair
    from codepilot.llm import Reply, Usage

    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    asked = []

    class Model:
        async def chat(self, messages, max_tokens=None, **kw):
            asked.append(max_tokens)
            if len(asked) == 1:
                return Reply('{"search": "ret', [], [], "max_tokens", "m", Usage(10, 10), 1, 0.001)
            text = json.dumps({"explanation": "fix", "search": "return a - b", "replace": "return a + b"})
            return Reply(text, [], [], "end_turn", "m", Usage(10, 10), 1, 0.001)

    loc = LocalizationResult(suspect_files=["calc.py"], suspect_locations=[], repo_map="")
    result = await repair(Model(), "m", tmp_path, "add subtracts", loc, num_samples=1)
    assert asked == [SAMPLE_MAX_TOKENS, SAMPLE_MAX_TOKENS * 2]
    assert result.retried == 1 and result.calls == 2
    assert result.samples[0].patched is not None and "a + b" in result.samples[0].patched


async def test_the_sample_budget_follows_the_output_cap_both_arms_are_given(tmp_path):
    """C2/D45: --max-output-tokens governed the agent only, so the agentless
    arm sampled at a hardcoded 4,096 while the agent had 2,048. The flag now
    sets both; only agentless re-asks, at double its own budget."""
    import json

    from codepilot.bench.agentless.localize import LocalizationResult
    from codepilot.bench.agentless.repair import repair
    from codepilot.llm import Reply, Usage

    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    asked = []

    class Model:
        async def chat(self, messages, max_tokens=None, **kw):
            asked.append(max_tokens)
            if len(asked) == 1:
                return Reply('{"search": "ret', [], [], "max_tokens", "m", Usage(10, 10), 1, 0.001)
            text = json.dumps({"explanation": "fix", "search": "return a - b", "replace": "return a + b"})
            return Reply(text, [], [], "end_turn", "m", Usage(10, 10), 1, 0.001)

    loc = LocalizationResult(suspect_files=["calc.py"], suspect_locations=[], repo_map="")
    await repair(Model(), "m", tmp_path, "add subtracts", loc, num_samples=1,
                 max_output_tokens=2048)
    assert asked == [2048, 4096]


async def test_the_arm_config_output_cap_reaches_the_agentless_sampler(monkeypatch):
    """The flag must arrive through the pipeline, not stop at the agent."""
    from codepilot.bench.agentless import pipeline
    from codepilot.bench.harness import ArmConfig

    seen = {}

    async def fake_repair(client, model, root, issue, loc, num_samples, *, seed=None,
                          max_output_tokens=None):
        seen["max_output_tokens"] = max_output_tokens
        from codepilot.bench.agentless.repair import RepairResult

        return RepairResult(samples=[])

    async def fake_localize(*a, **kw):
        from codepilot.bench.agentless.localize import LocalizationResult

        return LocalizationResult(suspect_files=[], suspect_locations=[], repo_map="")

    monkeypatch.setattr(pipeline, "repair", fake_repair)
    monkeypatch.setattr(pipeline, "localize", fake_localize)

    class Env:
        root = __import__("pathlib").Path(".")

        def restore(self):
            pass

        def diff(self):
            return ""

    cfg = ArmConfig(model="m", max_output_tokens=4096)
    await pipeline.run_agentless(Env(), object(), cfg.model, "issue", 1, seed=0,
                                 max_output_tokens=cfg.max_output_tokens)
    assert seen["max_output_tokens"] == 4096
