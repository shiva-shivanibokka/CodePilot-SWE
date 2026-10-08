"""Paired statistics for the funded agent-vs-agentless study.

STUDY_PLAN specifies "a bootstrap CI over instances (10,000 resamples,
instances resampled, seeds kept)". That was never computed: no confidence
interval of any kind appeared anywhere in this repository, only the exact
McNemar p-value. This script computes both from the committed
`bench/results/haiku-study/main.jsonl`, so neither number has to be taken on
trust or recomputed by hand.

It reads only committed data, calls no model, and spends nothing.

    python -m bench.analyze_study

Determinism: the resampling seed is fixed (--seed, default 0) and the
instance order is sorted, so repeated runs give identical intervals.

A note on what the interval can mean at n = 10. The bootstrap resamples 10
instances, so the resolve-rate difference can only land on multiples of 0.1 and
the interval is wide by construction. It is reported because the plan asked for
it and because a wide interval is the honest summary of this sample size -- not
because it adds precision.
"""
from __future__ import annotations

import argparse
import json
from math import comb
from pathlib import Path

STUDY = Path(__file__).resolve().parent / "results" / "haiku-study" / "main.jsonl"


def load_pairs(path: Path) -> dict[str, dict[str, bool]]:
    """{instance_id: {arm: resolved}} from the study's result rows.

    The first line of the file is the run's configuration header and carries no
    `instance_id`; rows without one are skipped rather than assumed to be
    results.
    """
    pairs: dict[str, dict[str, bool]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        iid, arm = row.get("instance_id"), row.get("arm")
        if not iid or not arm:
            continue
        pairs.setdefault(iid, {})[arm] = bool(row.get("resolved"))
    return pairs


def exact_mcnemar_two_sided(b: int, c: int) -> float:
    """Exact two-sided McNemar on the discordant pairs.

    Under H0 each discordant pair favours either arm with probability 0.5, so
    the count is Binomial(n = b + c, 0.5). The two-sided exact p is the total
    probability of outcomes at least as extreme as the observed split.
    """
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(comb(n, i) for i in range(0, k + 1)) * 0.5**n
    return min(1.0, 2.0 * tail)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--resamples", type=int, default=10_000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--path", type=Path, default=STUDY)
    args = ap.parse_args()

    pairs = load_pairs(args.path)
    ids = sorted(i for i, v in pairs.items() if "agent" in v and "agentless" in v)
    if not ids:
        print("no paired instances found")
        return 1

    agent = [pairs[i]["agent"] for i in ids]
    agentless = [pairs[i]["agentless"] for i in ids]
    n = len(ids)

    both = sum(1 for a, b in zip(agent, agentless) if a and b)
    agent_only = sum(1 for a, b in zip(agent, agentless) if a and not b)
    less_only = sum(1 for a, b in zip(agent, agentless) if b and not a)
    neither = sum(1 for a, b in zip(agent, agentless) if not a and not b)

    ra, rl = sum(agent) / n, sum(agentless) / n
    diff = ra - rl
    p = exact_mcnemar_two_sided(agent_only, less_only)

    # Bootstrap over instances. Pure-stdlib Mersenne Twister, seeded, so the
    # interval reproduces without numpy.
    import random

    rng = random.Random(args.seed)
    diffs = []
    for _ in range(args.resamples):
        idx = [rng.randrange(n) for _ in range(n)]
        da = sum(agent[j] for j in idx) / n
        dl = sum(agentless[j] for j in idx) / n
        diffs.append(da - dl)
    diffs.sort()

    def pct(q: float) -> float:
        # Nearest-rank percentile; adequate here and free of interpolation
        # assumptions on a lattice-valued statistic.
        pos = min(len(diffs) - 1, max(0, int(round(q * (len(diffs) - 1)))))
        return diffs[pos]

    lo, hi = pct(0.025), pct(0.975)

    print(f"instances (paired): {n}")
    print(f"agent resolved:     {sum(agent)}/{n}  ({ra:.3f})")
    print(f"agentless resolved: {sum(agentless)}/{n}  ({rl:.3f})")
    print(f"contingency: both {both}, agent only {agent_only}, "
          f"agentless only {less_only}, neither {neither}")
    print(f"paired difference (agent - agentless): {diff:+.3f}")
    print(f"exact McNemar two-sided p = {p:.4f}  "
          f"(smallest attainable with {agent_only + less_only} discordant "
          f"pairs: {exact_mcnemar_two_sided(agent_only + less_only, 0):.4f})")
    print(f"bootstrap {args.resamples} resamples, seed {args.seed}, "
          f"instances resampled")
    print(f"  95% percentile CI for the difference: [{lo:+.3f}, {hi:+.3f}]")
    print(f"  share of resamples <= 0: "
          f"{sum(1 for d in diffs if d <= 0) / len(diffs):.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
