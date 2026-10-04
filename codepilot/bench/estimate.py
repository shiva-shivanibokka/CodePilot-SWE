"""
Price a planned study from measured token counts.

    python -m codepilot.bench.estimate                       # from the carried-over recordings
    python -m codepilot.bench.estimate --results bench/results/smoke/run.jsonl

Token counts per (instance, arm) are averaged from measured runs, scaled to the
planned design, and priced per model with LiteLLM's cost map (falling back to
`codepilot.llm.PRICING` rows, which say where their numbers come from, first). Cache
discounts are **not** assumed: every input token is priced as uncached, so the
estimate is an upper bound on input cost for providers that cache.

Scaling, stated so it can be checked:
* agent: tokens per attempt x attempts;
* agentless: (tokens per call) x (1 localisation + N samples), where tokens per
  call is the measured total divided by the measured number of calls.
"""

from __future__ import annotations

import argparse
import glob
import json
import statistics
from pathlib import Path

from codepilot.llm import is_local, price_for

RECORDINGS = "bench/results/autonomous-swe-agent-recordings/*_*.json"
MODELS = [
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-4-5",
    "gpt-5.6-terra",
    "gemini/gemini-2.5-flash",
    "gemini/gemini-3-flash-preview",
    "groq/openai/gpt-oss-120b",
    "groq/llama-3.3-70b-versatile",
]


def price(model: str) -> tuple[float, float, str]:
    """USD per token (input, output), and where the number came from.

    Raises KeyError for a model with no price, including a price of 0 in
    LiteLLM's map: an unpriced model is never estimated at $0 (D26).
    """
    p = price_for(model)
    if p is None or (p.input <= 0 and not is_local(model)):
        raise KeyError(f"no price for {model}")
    return p.input, p.output, p.source


def measured_from_recordings(pattern: str = RECORDINGS) -> dict:
    """Per-arm means from Autonomous-SWE-Agent's recordings (B's own format)."""
    arms: dict[str, list[dict]] = {"agent": [], "agentless": []}
    for f in glob.glob(pattern):
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        calls = d["turns"] if d["approach"] == "agent" else 1 + int(d.get("candidates") or 0)
        arms[d["approach"]].append({"in": d["inputTokens"], "out": d["outputTokens"], "calls": calls})
    return _summarise(arms, attempts=1, samples={"agentless": 4})


def measured_from_results(path: str) -> dict:
    """Per-arm means from this repository's `bench.run` JSONL output."""
    arms: dict[str, list[dict]] = {"agent": [], "agentless": []}
    attempts = 1
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if r.get("arm") not in arms or r.get("infra_error"):
            continue
        attempts = r.get("attempts", 1)
        arms[r["arm"]].append({
            "in": r["input_tokens"] + r["cache_read_tokens"] + r["cache_write_tokens"],
            "out": r["output_tokens"], "calls": r["model_calls"],
        })
    return _summarise(arms, attempts=attempts, samples={"agentless": attempts})


def _summarise(arms, attempts, samples) -> dict:
    out = {}
    for arm, rows in arms.items():
        if not rows:
            continue
        mean_in = statistics.mean(r["in"] for r in rows)
        mean_out = statistics.mean(r["out"] for r in rows)
        calls = statistics.mean(r["calls"] for r in rows)
        if arm == "agent":
            out[arm] = {"in_per_unit": mean_in / attempts, "out_per_unit": mean_out / attempts,
                        "unit": "attempt", "n": len(rows)}
        else:
            out[arm] = {"in_per_unit": mean_in / calls, "out_per_unit": mean_out / calls,
                        "unit": "call", "n": len(rows)}
    return out


def plan_tokens(measured: dict, instances: int, seeds: int, attempts: int) -> dict:
    runs = instances * seeds
    tokens = {}
    if "agent" in measured:
        m = measured["agent"]
        tokens["agent"] = (runs * attempts * m["in_per_unit"], runs * attempts * m["out_per_unit"])
    if "agentless" in measured:
        m = measured["agentless"]
        calls = 1 + attempts
        tokens["agentless"] = (runs * calls * m["in_per_unit"], runs * calls * m["out_per_unit"])
    return tokens


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", help="bench.run JSONL to measure from (default: the recordings)")
    ap.add_argument("--instances", type=int, default=50)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--attempts", type=int, default=3)
    ap.add_argument("--hardness", type=float, default=1.0,
                    help="multiply measured tokens (the recordings are 4 hand-picked, easy instances)")
    args = ap.parse_args(argv)

    measured = measured_from_results(args.results) if args.results else measured_from_recordings()
    tokens = plan_tokens(measured, args.instances, args.seeds, args.attempts)
    total_in = sum(t[0] for t in tokens.values()) * args.hardness
    total_out = sum(t[1] for t in tokens.values()) * args.hardness

    print(f"measured: {json.dumps(measured, indent=None)}")
    print(f"design: {args.instances} instances x {args.seeds} seeds x arms {list(tokens)} "
          f"x {args.attempts} attempts/samples, hardness x{args.hardness}")
    for arm, (i, o) in tokens.items():
        print(f"  {arm:<9} {i * args.hardness / 1e6:8.2f}M in  {o * args.hardness / 1e6:6.2f}M out")
    print(f"  {'total':<9} {total_in / 1e6:8.2f}M in  {total_out / 1e6:6.2f}M out\n")
    print(f"{'model':<34} {'$/M in':>7} {'$/M out':>8} {'USD':>9}  price source")
    for model in MODELS:
        try:
            p_in, p_out, src = price(model)
        except KeyError:
            print(f"{model:<34} {'':>7} {'':>8} {'':>9}  no price: refused, not estimated at $0")
            continue
        usd = total_in * p_in + total_out * p_out
        print(f"{model:<34} {p_in * 1e6:7.2f} {p_out * 1e6:8.2f} {usd:9.2f}  {src}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# ---------------------------------------------------------------------------
# Worst case of a design, from its caps (D39)
# ---------------------------------------------------------------------------

#: Localisation has its own output cap (localize.py); a sample uses the arm's
#: `--max-output-tokens`, and its one re-ask double that (repair.py, D45).
LOCALIZE_OUTPUT = 2048


def agentless_outputs(max_output_tokens: int) -> dict[str, int]:
    return {"localize": LOCALIZE_OUTPUT, "sample": max_output_tokens,
            "reask": max_output_tokens * 2}


def worst_case_design(
    model: str,
    *,
    instances: int,
    seeds: int,
    attempts: int,
    arms: list[str],
    max_cost_per_attempt: float,
    max_prompt_tokens: int,
    max_output_tokens: int,
) -> dict[str, float]:
    """The most a design can cost, if every cap is reached.

    Agent: each attempt stops at its dollar budget, checked once per step, and
    a step can make two calls after the check — a compaction summary and the
    model call — so it can overshoot by two calls: `max_cost_per_attempt` plus
    two calls' worst case (a `max_prompt_tokens` prompt at the dearer of input
    and cache-write, plus `max_output_tokens` out; the compaction summary's
    own cap is 2,048, no more than the agent's in the planned design).

    Agentless: one localisation and, per sample, one repair call and its
    possible re-ask at double the output cap — each with a full prompt.
    """
    p = price_for(model)
    if p is None:
        raise KeyError(f"no price for {model}")
    prompt_cost = max_prompt_tokens * max(p.input, p.cache_write)
    runs = instances * seeds
    out: dict[str, float] = {"agent": 0.0, "agentless": 0.0}
    if "agent" in arms:
        per_call = prompt_cost + max_output_tokens * p.output
        compaction = prompt_cost + max(2048, max_output_tokens) * p.output
        out["agent"] = runs * attempts * (max_cost_per_attempt + per_call + compaction)
    if "agentless" in arms:
        o = agentless_outputs(max_output_tokens)
        calls = [o["localize"]] + [o["sample"], o["reask"]] * attempts
        out["agentless"] = runs * sum(prompt_cost + o * p.output for o in calls)
    out["total"] = out["agent"] + out["agentless"]
    return out
