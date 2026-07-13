"""
Record a real agent run to JSON for the frontend demo.

The hosted frontend ships a bundled SAMPLE run so the console works with no
backend. This script produces a REAL recording you can swap in: it runs the
agent on one GitHub issue (BYOK) and writes the event stream — with the original
inter-event timing — in the shape the frontend replayer expects.

Usage:
    python -m eval.record_run \
        --issue https://github.com/psf/requests/issues/5649 \
        --provider anthropic --model claude-sonnet-5 \
        --out frontend/lib/recorded-run.json

Then either import it in frontend/lib/replay.ts in place of SAMPLE_RUN, or serve
it from the backend and have the frontend fetch it. Requires Docker + a key in
the provider's env var (e.g. ANTHROPIC_API_KEY).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from agent.llm import LLMConfig  # noqa: E402
from agent.loop import run_agent  # noqa: E402
from agent.providers import PROVIDERS, key_env_for  # noqa: E402
from github_integration.issue_fetcher import fetch_issue  # noqa: E402
from sandbox.docker_workspace import DockerWorkspace  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Record a real agent run for the frontend demo.")
    ap.add_argument("--issue", required=True, help="GitHub issue URL")
    ap.add_argument("--provider", choices=sorted(PROVIDERS), default="anthropic")
    ap.add_argument("--model", default=None, help="Model id (default: provider's first)")
    ap.add_argument("--out", default="frontend/lib/recorded-run.json")
    args = ap.parse_args()

    provider = PROVIDERS[args.provider]
    model = args.model or provider.models[0].id
    api_key = os.getenv(key_env_for(args.provider))
    if not api_key:
        sys.exit(f"ERROR: set {key_env_for(args.provider)} in .env")

    llm = LLMConfig(provider=args.provider, model=model, api_key=api_key)

    print(f"Fetching {args.issue} …")
    issue = fetch_issue(args.issue)

    print(f"Running agent ({args.provider}/{model}) — this takes a few minutes …")
    raw: list[dict] = []
    with DockerWorkspace.create(issue.repo_url, issue.base_commit) as ws:
        gen = run_agent(ws, issue.issue_text, llm)
        try:
            while True:
                raw.append(next(gen).to_dict())
        except StopIteration:
            pass

    # Convert absolute timestamps into per-event replay delays (capped so a long
    # think doesn't stall the demo).
    events = []
    prev = raw[0]["timestamp"] if raw else 0.0
    for e in raw:
        delay = min(max(int((e["timestamp"] - prev) * 1000), 150), 2500)
        prev = e["timestamp"]
        events.append({"type": e["type"], "turn": e.get("turn", 0), "delayMs": delay, "data": e["data"]})

    out = {
        "issueUrl": args.issue,
        "title": issue.issue_title,
        "approach": "agent",
        "events": events,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2))
    print(f"Wrote {len(events)} events to {out_path}")


if __name__ == "__main__":
    main()
