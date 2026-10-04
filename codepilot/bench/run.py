"""
Run SWE-bench Lite instances through the arms and write the raw results.

    # Check the harness itself on an instance: no model, no key.
    python -m codepilot.bench.run --instances pallets__flask-4992 --arms gold empty

    # Both arms, one attempt each, on a free-tier model
    python -m codepilot.bench.run --instances pallets__flask-4992 --arms agent agentless \\
        --model gemini/gemini-2.5-flash --setups bench/setups.json

    # Budget-matched: 3 agent attempts vs 3 agentless samples, 50 random instances
    python -m codepilot.bench.run --sample 50 --seed 0 --attempts 3 --arms agent agentless ...

Two arms exist only to check the harness, and spend nothing:

* `gold`  — submits the instance's own fix. Must resolve, or the environment
  or the grader is broken for that instance and no arm's result on it means
  anything.
* `empty` — submits nothing. Must not resolve.

Results are written one JSON object per line as each (instance, arm) finishes,
so a crash or a quota cut-off loses nothing already done.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from codepilot.bench import swebench
from codepilot.bench.environment import BenchEnv, swebench_image
from codepilot.bench.harness import ARMS, ArmConfig, InstanceResult, run_instance
from codepilot.bench.runlock import RunLock, RunLocked, break_stale_lock
from codepilot.llm import AbortRun, Ledger, is_local, price_for

CHECK_ARMS = ("gold", "empty")

#: Results are meant to be committed. Anything key-shaped is redacted before a
#: row is written, whatever path it took to get there (a command's output, an
#: error message quoting a request). From Autonomous-SWE-Agent's recorder
#: (`eval/record_run.py::scan_for_secrets`), which refused to write instead.
SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{32,}"),
    re.compile(r"gsk_[A-Za-z0-9]{20,}"),
    re.compile(r"AIza[A-Za-z0-9_\-]{30,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
]


def redact(text: str) -> tuple[str, int]:
    """`text` with key-shaped substrings replaced, and how many there were."""
    count = 0
    for pattern in SECRET_PATTERNS:
        text, n = pattern.subn("[REDACTED]", text)
        count += n
    return text, count


def choose_instances(args) -> list[dict]:
    if args.instances:
        return swebench.load_instances(args.instances)
    # The frozen dataset, in a seeded order (instances.seeded_order): the same
    # --seed always gives the same instances, offline (D37).
    from codepilot.bench.instances import sample

    return sample(args.sample, args.seed)


#: The most this project may ever spend on paid model calls, across every run
#: and every checkout (the user-level ledger holds them all). Equal to the
#: planned cap in bench/STUDY_PLAN.md; a higher --max-total-usd is refused.
PROJECT_MAX_USD = 20.0


def ledger_path(out: str | Path) -> Path:
    """The ledger for a run writing to `out`: always the user-level one (D41)."""
    return Ledger.default().path


def dry_run(args, model_arms: list[str]) -> int:
    """Price the planned design at its caps, before anything runs (D39)."""
    from codepilot.bench.estimate import worst_case_design

    n = len(args.instances) if args.instances else args.sample
    worst = worst_case_design(
        args.model, instances=n, seeds=1, attempts=args.attempts, arms=model_arms,
        max_cost_per_attempt=args.max_cost, max_prompt_tokens=args.max_prompt_tokens,
        max_output_tokens=args.max_output_tokens,
    )
    already = Ledger.default().total_usd()
    print(f"dry run: {n} instance(s) x arms {model_arms} x {args.attempts} attempt(s) on {args.model}")
    print(f"  caps: ${args.max_cost:.2f}/agent attempt, {args.max_prompt_tokens:,} prompt tokens/call, "
          f"{args.max_output_tokens:,} output tokens/agent call")
    for arm in model_arms:
        print(f"  worst case {arm:<9} ${worst[arm]:.2f}")
    print(f"  worst case total     ${worst['total']:.2f}  (+ ${already:.2f} already in the ledger)")
    if args.max_total_usd is not None and worst["total"] + already > args.max_total_usd:
        print(f"  ! exceeds --max-total-usd ${args.max_total_usd:.2f}: refusing")
        return 2
    return 0


def load_keys(paths: list[str]) -> None:
    """Load provider keys into this process only. Never printed."""
    from dotenv import load_dotenv

    for p in paths:
        if Path(p).is_file():
            load_dotenv(p, override=False)


async def check_harness(instance: dict, arms: list[str], *, backend, setup, image, python,
                        env_options: dict | None = None) -> list[dict]:
    repo_url = instance.get("repo_url") or f"https://github.com/{instance['repo']}.git"
    if backend == "docker" and image == "official":
        image = swebench_image(instance["instance_id"])
    out = []
    env = await BenchEnv.create(
        repo_url, instance["base_commit"], backend=backend, setup=setup, image=image,
        python=python, task_id=instance["instance_id"], **(env_options or {}),
    )
    try:
        for arm in arms:
            diff = instance.get("patch", "") if arm == "gold" else ""
            report = await swebench.grade(env, instance, diff, run_if_empty=True)
            env.restore()
            out.append(
                {
                    "instance_id": instance["instance_id"],
                    "arm": arm,
                    "resolved": report.resolved,
                    "expected": arm == "gold",
                    "ok": report.resolved == (arm == "gold"),
                    "grade": report.to_dict(),
                    "backend": env.backend,
                    "image": env.image,
                    "setup": [asdict(s) for s in env.setup],
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            )
    finally:
        await env.close()
    return out


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--instances", nargs="+", help="SWE-bench Lite instance ids")
    target.add_argument("--sample", type=int, help="this many instances, sampled with --seed")
    target.add_argument("--break-stale-lock", action="store_true", dest="break_stale_lock",
                        help="remove a run lock whose process is no longer running, then exit")
    ap.add_argument("--seed", type=int, default=0,
                    help="sampling seed for --sample, and the run seed sent to the model")
    ap.add_argument("--no-model-seed", action="store_true",
                    help="do not send a seed to the model")
    ap.add_argument("--api-base", default=None, help="a self-hosted endpoint, e.g. for Ollama")
    ap.add_argument("--model-option", action="append", default=[], metavar="KEY=VALUE",
                    help="provider option sent with every request, e.g. num_ctx=16384")
    ap.add_argument("--max-output-tokens", type=int, default=8192, dest="max_output_tokens",
                    help="output tokens per agent call")
    ap.add_argument("--arms", nargs="+", default=["agent", "agentless"], choices=[*ARMS, *CHECK_ARMS])
    ap.add_argument("--model", default=os.getenv("CODEPILOT_BENCH_MODEL", "gemini/gemini-2.5-flash"))
    ap.add_argument("--attempts", type=int, default=1,
                    help="agent attempts and agentless samples per instance (budget-matched)")
    ap.add_argument("--backend", choices=["local", "docker"], default="local")
    ap.add_argument("--image", default=None,
                    help="docker image; 'official' = the instance's swebench/sweb.eval image")
    ap.add_argument("--python", default=None, help="interpreter for the local backend's venv")
    ap.add_argument("--setups", default=None, help="JSON file: instance id -> setup command")
    ap.add_argument("--max-cost", type=float, default=1.00, dest="max_cost",
                    help="USD per agent attempt, at list price")
    ap.add_argument("--max-calls", type=int, default=40, dest="max_calls")
    ap.add_argument("--max-tokens", type=int, default=800_000, dest="max_tokens")
    ap.add_argument("--compact-at", type=int, default=100_000, dest="compact_at")
    ap.add_argument("--env-file", action="append", default=[],
                    help="a .env to load provider keys from (repeatable; never printed)")
    ap.add_argument("--out", default="bench/results/run.jsonl")
    ap.add_argument("--max-total-usd", type=float, default=None, dest="max_total_usd",
                    help="hard cap on the whole run's spend, both arms, at list price; "
                         "includes what the ledger already records (D28)")
    ap.add_argument("--max-prompt-tokens", type=int, default=50_000, dest="max_prompt_tokens",
                    help="refuse any request estimated above this (the arm fails); bounds "
                         "every call's worst case (D39)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the design's worst-case cost and stop; exit 2 if it exceeds "
                         "--max-total-usd")
    ap.add_argument("--response-cache", default=None, dest="response_cache",
                    help="directory of stored replies; a rerun is served from it at $0 (D33)")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    if args.break_stale_lock:
        try:
            print(break_stale_lock())
        except RunLocked as exc:
            print(f"  ! {exc}")
            return 4
        return 0

    model_arms_requested = [a for a in args.arms if a in ARMS]
    if model_arms_requested and price_for(args.model) is None:
        # A paid model with no price would run with no dollar ceiling (D26).
        print(f"  ! {args.model} has no price (codepilot.llm.PRICING or LiteLLM's map); "
              "refusing to run a model whose spend cannot be capped.")
        return 2
    paid = bool(model_arms_requested) and not is_local(args.model)
    if paid:
        if args.max_total_usd is None:
            print("  ! a paid model needs --max-total-usd (the run-wide cap, D28)")
            return 2
        if args.max_total_usd > PROJECT_MAX_USD:
            print(f"  ! --max-total-usd ${args.max_total_usd:.2f} is above this project's hard "
                  f"maximum ${PROJECT_MAX_USD:.2f} (D41)")
            return 2
    if args.dry_run:
        return dry_run(args, model_arms_requested)
    lock = RunLock() if paid else None
    if lock is not None:
        try:
            lock.acquire()
        except RunLocked as exc:
            print(f"  ! {exc}")
            return 4
    try:
        return await _run(args, model_arms_requested)
    finally:
        if lock is not None:
            lock.release()


async def _run(args, model_arms_requested: list[str]) -> int:
    load_keys(args.env_file)
    setups = json.loads(Path(args.setups).read_text(encoding="utf-8")) if args.setups else {}
    instances = choose_instances(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    def write(row: dict) -> None:
        line, hits = redact(json.dumps(row))
        if hits:
            print(f"    redacted {hits} key-shaped string(s) from the result row")
        with out.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    model_arms = [a for a in args.arms if a in ARMS]
    check_arms = [a for a in args.arms if a in CHECK_ARMS]
    cfg = ArmConfig(
        model=args.model, attempts=args.attempts, max_usd=args.max_cost,
        max_turns=args.max_calls, max_tokens=args.max_tokens, compact_at=args.compact_at,
        seed=None if args.no_model_seed else args.seed,
        max_output_tokens=args.max_output_tokens,
    )
    extra = {}
    for item in args.model_option:
        key, _, value = item.partition("=")
        extra[key] = int(value) if value.isdigit() else value
    from codepilot.llm import LLMClient

    ledger_file = ledger_path(out)
    client = LLMClient(model=args.model, api_base=args.api_base, extra=extra,
                       ledger=Ledger(ledger_file), max_total_usd=args.max_total_usd,
                       max_prompt_tokens=args.max_prompt_tokens,
                       response_cache=args.response_cache)
    if args.max_total_usd is not None:
        print(f"spend cap ${args.max_total_usd:.2f}; already in the ledger "
              f"${client.total_spent_usd():.4f}")
    print(f"every model request is appended to {ledger_file}")
    print(f"{len(instances)} instance(s) x arms {args.arms} -> {out}")
    if model_arms:
        from codepilot.bench.harness import cache_prefix_report

        prefix = cache_prefix_report(args.model)
        for name, p in prefix["prefixes"].items():
            print(f"  cache: {name} prefix ~{p['estimated_tokens']} tokens; "
                  f"{args.model} caches from {prefix['minimum_cacheable_tokens']}; "
                  f"on its own: {p['caches_on_its_own']}")
        write({"arm": "config", "model": args.model, "arms": model_arms, "attempts": args.attempts,
               "seed": args.seed, "backend": args.backend, "image": args.image,
               "max_total_usd": args.max_total_usd, "max_cost_per_attempt": args.max_cost,
               "max_calls": args.max_calls, "max_tokens": args.max_tokens,
               "max_output_tokens": args.max_output_tokens, "instances": [i["instance_id"] for i in instances],
               "cache_prefix": prefix, "timestamp": datetime.now(UTC).isoformat()})
    for n, inst in enumerate(instances, 1):
        iid = inst["instance_id"]
        setup = setups.get(iid)
        print(f"[{n}/{len(instances)}] {iid}", flush=True)
        try:
            if check_arms:
                for row in await check_harness(inst, check_arms, backend=args.backend, setup=setup,
                                               image=args.image, python=args.python):
                    write(row)
                    print(f"    {row['arm']:<9} resolved={row['resolved']}  {'ok' if row['ok'] else 'HARNESS PROBLEM'}")
            if model_arms:
                def report(r: InstanceResult) -> None:
                    write(r.to_dict())
                    flag = " (infra error: excluded)" if r.infra_error else (" error" if r.error else "")
                    print(f"    {r.arm:<9} resolved={r.resolved}  ${r.cost_usd:.4f}  "
                          f"{r.model_calls} calls  {r.wall_seconds:.0f}s{flag}", flush=True)

                await run_instance(inst, model_arms, cfg, backend=args.backend, setup=setup,
                                   image=args.image, python=args.python, on_result=report,
                                   client=client)
        except AbortRun as exc:
            write({"instance_id": iid, "arm": "aborted", "error": f"{type(exc).__name__}: {exc}"[:2000],
                   "total_spent_usd": round(client.total_spent_usd(), 6),
                   "timestamp": datetime.now(UTC).isoformat()})
            print(f"    run aborted: {exc}")
            return 3
        except Exception as exc:  # noqa: BLE001 - environment failures are results too
            spent = client.spent(f"{iid}:")
            row = {"instance_id": iid, "arm": "environment", "error": f"{type(exc).__name__}: {exc}"[:2000],
                   # Whatever the arms spent before the environment failed (D27).
                   "cost_usd": round(spent.cost_usd, 6), "model_calls": spent.calls,
                   "timestamp": datetime.now(UTC).isoformat()}
            write(row)
            print(f"    environment failed: {row['error'][:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
