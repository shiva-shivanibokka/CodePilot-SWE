"""The paired statistics reported for the funded study must be reproducible.

Every number in bench/results/haiku-study/RESULTS.md's "What this does and does
not support" section comes out of bench/analyze_study.py. If the script drifts
from the committed study data, the write-up silently becomes wrong, so the
headline counts, the contingency, the McNemar p and the bootstrap bounds are all
pinned here.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from bench.analyze_study import STUDY, exact_mcnemar_two_sided, load_pairs

ROOT = Path(__file__).resolve().parents[1]


def test_the_study_data_still_holds_ten_paired_instances():
    pairs = load_pairs(STUDY)
    paired = {i: v for i, v in pairs.items() if "agent" in v and "agentless" in v}
    assert len(paired) == 10
    assert sum(v["agent"] for v in paired.values()) == 5
    assert sum(v["agentless"] for v in paired.values()) == 1


def test_the_contingency_is_one_four_zero_five():
    pairs = load_pairs(STUDY)
    vals = [
        (v["agent"], v["agentless"])
        for v in pairs.values()
        if "agent" in v and "agentless" in v
    ]
    both = sum(1 for a, b in vals if a and b)
    agent_only = sum(1 for a, b in vals if a and not b)
    less_only = sum(1 for a, b in vals if b and not a)
    neither = sum(1 for a, b in vals if not a and not b)
    assert (both, agent_only, less_only, neither) == (1, 4, 0, 5)


def test_the_config_header_row_is_not_counted_as_a_result():
    """main.jsonl's first line is the run configuration and has no instance_id.

    Counting it would add a phantom instance, so rows without an id or an arm
    are skipped rather than assumed to be results.
    """
    first = json.loads(STUDY.read_text(encoding="utf-8").splitlines()[0])
    assert "instance_id" not in first
    assert STUDY.name not in load_pairs(STUDY)


@pytest.mark.parametrize(
    "b, c, expected",
    [
        (4, 0, 0.125),   # the study's split, and this design's floor
        (0, 0, 1.0),     # no discordant pairs says nothing
        (1, 0, 1.0),     # 2 * 0.5 clipped to 1
        (5, 0, 0.0625),  # one more discordant pair would have cleared 0.05
        (3, 1, 0.625),
    ],
)
def test_exact_mcnemar_two_sided(b, c, expected):
    assert exact_mcnemar_two_sided(b, c) == pytest.approx(expected)


def test_mcnemar_is_symmetric_in_its_arms():
    assert exact_mcnemar_two_sided(4, 1) == exact_mcnemar_two_sided(1, 4)


def test_the_reported_bootstrap_interval_reproduces():
    """RESULTS.md quotes +0.400 with a 95% CI of [+0.100, +0.700] at seed 0."""
    out = subprocess.run(
        [sys.executable, "-m", "bench.analyze_study", "--seed", "0"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    assert "paired difference (agent - agentless): +0.400" in out
    assert "95% percentile CI for the difference: [+0.100, +0.700]" in out
    assert "exact McNemar two-sided p = 0.1250" in out


def test_the_bootstrap_is_deterministic_for_a_given_seed():
    runs = [
        subprocess.run(
            [sys.executable, "-m", "bench.analyze_study", "--seed", "7",
             "--resamples", "2000"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout
        for _ in range(2)
    ]
    assert runs[0] == runs[1]
