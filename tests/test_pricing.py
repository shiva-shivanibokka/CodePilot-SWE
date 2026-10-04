"""Prices: pinned, explicit, and never silently zero."""

from __future__ import annotations

import subprocess
import sys

import pytest


def test_importing_codepilot_pins_litellm_to_its_bundled_cost_map():
    """Without LITELLM_LOCAL_MODEL_COST_MAP, LiteLLM downloads its cost map
    from GitHub at import, so the prices a run used depend on when it ran."""
    out = subprocess.run(
        [sys.executable, "-c",
         "import os, codepilot, litellm; print(os.environ.get('LITELLM_LOCAL_MODEL_COST_MAP'))"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out == "True"


def test_the_installed_litellm_is_the_pinned_one():
    from importlib.metadata import version

    assert version("litellm") == "1.103.2"


# ------------------------------------------------------------------ prices
#
# Reproduced before the fix: `claude-opus-5-5` had no row in PRICING and no
# entry in LiteLLM 1.103.2's map, so every call was "unpriced" (cost None) and
# the per-attempt dollar budget never advanced: a paid run on the current
# default model would have run with no dollar ceiling at all.


def test_the_current_claude_models_are_priced():
    from codepilot.llm import price_for

    opus = price_for("claude-opus-5-5")
    assert opus is not None
    assert (opus.input, opus.output, opus.cache_read, opus.cache_write) == (4e-6, 20e-6, 0.2e-6, 5e-6)
    sonnet = price_for("anthropic/claude-sonnet-5-5")
    assert (sonnet.input, sonnet.output, sonnet.cache_read, sonnet.cache_write) == (2e-6, 10e-6, 0.2e-6, 2.5e-6)
    haiku = price_for("claude-haiku-4-5-20251001")
    assert (haiku.input, haiku.output, haiku.cache_read, haiku.cache_write) == (1e-6, 5e-6, 0.1e-6, 1.25e-6)
    assert all(p.source for p in (opus, sonnet, haiku))


def test_a_zero_or_missing_price_counts_as_no_price(monkeypatch):
    import litellm

    from codepilot.llm import price_for

    monkeypatch.setattr(litellm, "get_model_info", lambda model: {
        "input_cost_per_token": 0.0, "output_cost_per_token": 0.0})
    assert price_for("someprovider/free-looking-model") is None
    monkeypatch.setattr(litellm, "get_model_info", lambda model: {
        "input_cost_per_token": None, "output_cost_per_token": 1e-6})
    assert price_for("someprovider/half-priced-model") is None


def test_cost_counts_cache_reads_and_writes_at_their_own_prices():
    from codepilot.llm import Usage, cost_of

    usage = Usage(input_tokens=1000, output_tokens=100, cache_read_tokens=10_000, cache_write_tokens=2000)
    expected = 1000 * 1e-6 + 100 * 5e-6 + 10_000 * 0.1e-6 + 2000 * 1.25e-6
    assert abs(cost_of("claude-haiku-4-5", usage) - expected) < 1e-12
    assert cost_of("ollama/qwen2.5:7b", usage) == 0.0, "a local model is free, explicitly"
    assert cost_of("nobody/unknown", usage) is None


def test_a_paid_run_on_an_unpriced_model_is_refused_before_anything_starts(capsys):
    import asyncio

    from codepilot.bench.run import main

    code = asyncio.run(main(["--instances", "x__y-1", "--arms", "agent",
                             "--model", "nobody/unknown-model"]))
    assert code == 2
    assert "no price" in capsys.readouterr().out


def test_the_estimate_refuses_to_price_an_unpriced_model(capsys):
    from codepilot.bench.estimate import price

    try:
        price("nobody/unknown-model")
    except KeyError:
        pass
    else:
        raise AssertionError("an unpriced model must not be priced at $0")


# ------------------------------------------------------------ dry run (D39)


def test_the_worst_case_of_a_design_is_computed_from_the_caps():
    from codepilot.bench.estimate import worst_case_design

    w = worst_case_design("claude-haiku-4-5", instances=20, seeds=1, attempts=1,
                          arms=["agent", "agentless"], max_cost_per_attempt=0.40,
                          max_prompt_tokens=50_000, max_output_tokens=2048)
    per_call_agent = 50_000 * 1.25e-6 + 2048 * 5e-6
    # A step can make a compaction call and a model call after one budget check.
    assert w["agent"] == pytest.approx(20 * (0.40 + 2 * per_call_agent))
    # localise (2048 out) + one sample (4096) + its re-ask (8192), each with a full prompt
    per_instance_agentless = sum(50_000 * 1.25e-6 + out * 5e-6 for out in (2048, 4096, 8192))
    assert w["agentless"] == pytest.approx(20 * per_instance_agentless)
    assert w["total"] == pytest.approx(w["agent"] + w["agentless"])


def test_a_dry_run_refuses_a_design_whose_worst_case_exceeds_the_cap(capsys):
    import asyncio

    from codepilot.bench.run import main

    code = asyncio.run(main(["--sample", "20", "--seed", "0", "--arms", "agent", "agentless",
                             "--model", "claude-haiku-4-5", "--dry-run", "--max-total-usd", "5"]))
    assert code == 2
    assert "worst case" in capsys.readouterr().out
    code = asyncio.run(main(["--sample", "20", "--seed", "0", "--arms", "agent", "agentless",
                             "--model", "claude-haiku-4-5", "--dry-run", "--max-total-usd", "20",
                             "--max-cost", "0.40", "--max-output-tokens", "2048"]))
    assert code == 0


async def test_a_prompt_over_the_bound_is_refused_not_sent(monkeypatch):
    import litellm

    from codepilot.llm import LLMClient, LLMError

    async def fake(**params):
        raise AssertionError("must not be sent")

    monkeypatch.setattr(litellm, "acompletion", fake)
    client = LLMClient(model="claude-haiku-4-5", max_prompt_tokens=1000)
    with pytest.raises(LLMError, match="prompt bound"):
        await client.chat([{"role": "user", "content": "x" * 10_000}], max_tokens=10)
