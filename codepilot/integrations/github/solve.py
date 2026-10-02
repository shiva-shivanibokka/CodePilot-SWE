"""
A GitHub issue in, CodePilot's loop on it, a diff (and optionally a PR) out.

    python -m codepilot.integrations.github.solve https://github.com/o/r/issues/12 \\
        --model gemini/gemini-2.5-flash            # prints the diff
    ... --open-pr                                  # also pushes a branch and opens a PR

Autonomous-SWE-Agent's issue fetcher and PR creator, driven by the merged
agent instead of the deleted one. The repository is checked out the same way
as a benchmark task (`bench/checkout.clone_at`: the default branch's HEAD, no
remote) and the agent gets the same system prompt as the benchmark's agent
arm. Opening a PR needs GITHUB_TOKEN with write access, pushes a branch to the
repository named in the issue, and is never done without `--open-pr`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from codepilot.bench.environment import BenchEnv
from codepilot.bench.harness import ArmConfig, Spend, agent_attempt
from codepilot.events import EventStream
from codepilot.integrations.github.issue_fetcher import fetch_issue


async def solve(url: str, cfg: ArmConfig, *, client=None, open_pr: bool = False,
                fetch=fetch_issue, create=None, env_options: dict | None = None) -> dict:
    from codepilot.llm import LLMClient

    issue = fetch(url)
    client = client or LLMClient(model=cfg.model)
    env = await BenchEnv.create(issue.repo_url, issue.base_commit, task_id=f"issue-{issue.issue_number}",
                                **(env_options or {}))
    try:
        spend = Spend()
        diff, stopped_by, error = await agent_attempt(
            env, client, cfg, issue.issue_text, 0, spend, EventStream(session_id=url)
        )
        out = {"diff": diff, "stopped_by": stopped_by, "error": repr(error) if error else "",
               "cost_usd": spend.cost_usd, "model_calls": spend.calls, "pr": None}
        if open_pr and diff.strip() and error is None:
            if create is None:
                from codepilot.integrations.github.pr_creator import create_pr as create
            pr = create(issue, diff, f"Stopped: {stopped_by}.", repo_local_path=str(env.root))
            out["pr"] = pr.pr_url
        return out
    finally:
        await env.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("issue_url")
    ap.add_argument("--model", default="gemini/gemini-2.5-flash")
    ap.add_argument("--max-cost", type=float, default=1.0)
    ap.add_argument("--open-pr", action="store_true")
    args = ap.parse_args(argv)
    result = asyncio.run(solve(args.issue_url, ArmConfig(model=args.model, max_usd=args.max_cost),
                               open_pr=args.open_pr))
    sys.stdout.write(result["diff"] or "(no change)\n")
    print(f"\nstopped by {result['stopped_by']}; {result['model_calls']} calls; ${result['cost_usd']:.4f}"
          + (f"; PR {result['pr']}" if result["pr"] else ""))
    return 0 if result["diff"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
