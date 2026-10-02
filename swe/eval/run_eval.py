"""
CLI entry point for running the SWE-bench-lite evaluation.

Usage:
    # Run agent on 10 instances (cheap test)
    python -m eval.run_eval --approach agent --limit 10

    # Run agentless on 10 instances
    python -m eval.run_eval --approach agentless --limit 10

    # Run both and compare (full 300-instance eval)
    python -m eval.run_eval --compare

    # Run both on 10 instances for quick comparison
    python -m eval.run_eval --compare --limit 10

    # Use specific worker count
    python -m eval.run_eval --approach agent --limit 50 --workers 8
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).parent.parent / ".env")

from agent.llm import LLMConfig
from agent.providers import PROVIDERS, key_env_for
from eval.harness import (
    EVAL_INSTANCE_LIMIT,
    EVAL_MAX_WORKERS,
    load_swebench_lite,
    print_comparison_table,
    run_evaluation,
)
from observability.metrics import start_metrics_server
from observability.tracing import setup_tracing


def main():
    parser = argparse.ArgumentParser(
        description="Run SWE-bench-lite evaluation for agentic and/or agentless approach"
    )
    parser.add_argument(
        "--approach",
        choices=["agent", "agentless"],
        help="Which approach to evaluate",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Run both approaches and produce comparison table",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=f"Number of instances to evaluate (default: {EVAL_INSTANCE_LIMIT})",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=EVAL_MAX_WORKERS,
        help=f"Parallel workers (default: {EVAL_MAX_WORKERS})",
    )
    parser.add_argument(
        "--provider",
        choices=sorted(PROVIDERS),
        default="anthropic",
        help="LLM provider (default: anthropic)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model id (default: the provider's first listed model)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key (default: the provider's key env var, e.g. ANTHROPIC_API_KEY / OPENAI_API_KEY)",
    )

    args = parser.parse_args()

    if not args.approach and not args.compare:
        parser.error("Must specify --approach or --compare")

    provider = PROVIDERS[args.provider]
    model = args.model or provider.models[0].id
    key_env = key_env_for(args.provider)
    api_key = args.api_key or os.getenv(key_env)
    if not api_key:
        print(f"ERROR: no API key. Set {key_env} in .env or pass --api-key")
        sys.exit(1)
    llm = LLMConfig(provider=args.provider, model=model, api_key=api_key)
    print(f"Using {args.provider}/{model}")

    # Set up observability
    setup_tracing(service_name="swe-agent-eval")
    start_metrics_server()

    limit = args.limit or EVAL_INSTANCE_LIMIT
    print(f"\nLoading SWE-bench-lite (limit={limit})...")
    instances = load_swebench_lite(limit=limit)
    print(f"Loaded {len(instances)} instances")

    if args.compare:
        print(f"\nRunning AGENT approach ({len(instances)} instances, {args.workers} workers)...")
        agent_run = run_evaluation("agent", instances, llm, max_workers=args.workers)

        print(
            f"\nRunning AGENTLESS approach ({len(instances)} instances, {args.workers} workers)..."
        )
        agentless_run = run_evaluation(
            "agentless", instances, llm, max_workers=args.workers
        )

        print_comparison_table(agent_run, agentless_run)

    elif args.approach:
        print(
            f"\nRunning {args.approach.upper()} approach ({len(instances)} instances, {args.workers} workers)..."
        )
        run = run_evaluation(args.approach, instances, llm, max_workers=args.workers)

        print(f"\n{'=' * 50}")
        print(f"RESULTS — {args.approach.upper()}")
        print(f"{'=' * 50}")
        print(f"Resolved:    {run.resolved_count}/{run.total_instances} ({run.resolve_rate}%)")
        print(f"Total cost:  ${run.total_cost_usd:.2f}")
        print(f"Avg cost:    ${run.avg_cost_usd:.4f}/issue")
        print(f"Avg turns:   {run.avg_turns}")
        print(f"Results:     eval/results/{run.run_id}_summary.json")


if __name__ == "__main__":
    main()
