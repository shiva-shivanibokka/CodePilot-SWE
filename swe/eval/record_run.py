"""
Record one real agent run and write it where the frontend can replay it.

Live runs cannot be hosted: a run needs several minutes, a checkout of a
third-party repo, and a shell — none of which a free static host provides. The
honest alternative to a dead "Run" button is to run the agent for real once,
commit exactly what happened, and replay that. This script is what produces it.

Everything in the output is measured: the model's own words, the tool calls it
chose, the token counts and dollar cost the provider billed, the diff the agent
produced, and the result of running tests against that diff afterwards. Only
playback speed is synthetic — long pauses are capped so a run of several
minutes is watchable in half a minute.

Two ways to name the task:

    --instance pallets__flask-4992      a SWE-bench-lite instance, graded
                                        against its own FAIL_TO_PASS /
                                        PASS_TO_PASS tests
    --issue https://github.com/o/r/issues/1   any live GitHub issue, checked out
                                        at the default branch's HEAD

Examples:
    python -m eval.record_run --instance pallets__flask-4992 --backend local \\
        --setup 'python -m pip install -q -r requirements/tests.txt "werkzeug<2.3" "pytest>=7.4,<8"' \\
        --pytest-args '-W ignore::DeprecationWarning'

    python -m eval.record_run --issue https://github.com/owner/repo/issues/123

Requires a provider key in .env (ANTHROPIC_API_KEY by default) and, for
--backend docker, a working Docker daemon.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(PROJECT_ROOT / ".env", override=True)

from agent.llm import LLMConfig  # noqa: E402
from agent.loop import changed_lines, run_agent  # noqa: E402
from agent.providers import PROVIDERS, key_env_for  # noqa: E402
from agentless.pipeline import run_agentless  # noqa: E402
from eval.harness import (  # noqa: E402
    apply_test_patch,
    build_test_command,
    load_difficulty_labels,
    load_instance,
)
from sandbox import create_workspace  # noqa: E402

DEMO_DIR = PROJECT_ROOT / "frontend" / "public" / "demo"

# Playback pacing. A real run spends minutes waiting on the model and on test
# suites; replaying that faithfully would be unwatchable, so gaps are clamped.
MIN_DELAY_MS = 120
MAX_DELAY_MS = 2200

# A recording is committed to a public repo. Refuse to write one carrying
# anything key-shaped, whatever path it took to get there.
SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{32,}"),
    re.compile(r"gsk_[A-Za-z0-9]{20,}"),
    re.compile(r"AIza[A-Za-z0-9_\-]{30,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
]


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Record a real agent run for the frontend replay.")
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--instance", help="SWE-bench-lite instance id, e.g. pallets__flask-4992")
    target.add_argument("--issue", help="GitHub issue URL")

    ap.add_argument(
        "--approach",
        choices=["agent", "agentless"],
        default="agent",
        help="Which arm to record: the tool-driven loop, or the three-phase pipeline.",
    )
    ap.add_argument("--provider", choices=sorted(PROVIDERS), default="anthropic")
    ap.add_argument("--model", default=None, help="Model id (default: the provider's first)")
    ap.add_argument(
        "--backend",
        choices=["local", "docker"],
        default=os.getenv("WORKSPACE_BACKEND", "docker"),
        help="Execution backend. 'local' runs on this machine with no isolation.",
    )
    ap.add_argument("--max-turns", type=int, default=None, help="Cap on agent turns")
    ap.add_argument(
        "--samples",
        type=int,
        default=5,
        help="Agentless only: how many candidate patches to sample in phase 2.",
    )
    ap.add_argument(
        "--validate-cmd",
        # No `-x`: the gate compares pass/fail counts against a baseline, and
        # stopping at the first failure truncates both sides of that comparison.
        default="python -m pytest {scope} -q --tb=no 2>&1",
        help=(
            "Agentless only: how a candidate is checked in phase 3. `{scope}` is "
            "replaced with the test directory nearest the file being patched."
        ),
    )
    ap.add_argument(
        "--setup",
        default=None,
        help="Command run after checkout, before the agent — environment pins etc.",
    )
    ap.add_argument(
        "--pytest-args",
        default="",
        help="Extra flags appended to the grading pytest command.",
    )
    ap.add_argument(
        "--verify",
        default=None,
        help="For --issue runs: command used to check the agent's patch.",
    )
    ap.add_argument(
        "--allow-empty",
        action="store_true",
        help="Write the recording even if the agent produced no diff.",
    )
    return ap.parse_args()


def headline(problem_statement: str) -> str:
    """The issue's first meaningful line, for use as a title."""
    for line in problem_statement.strip().splitlines():
        line = line.strip().lstrip("#").strip()
        if len(line) > 3:
            return line[:110]
    return ""


def write_manifest() -> None:
    """
    Rebuild demo/index.json from whatever recordings are on disk.

    The site lists the runs before it loads any of them, so the manifest holds
    only what the picker needs — id, title, difficulty, verdict, headline
    figures. The events and the diff, which are the bulk of a recording, stay in
    the per-run file and are fetched when a visitor actually chooses that run.
    """
    runs = []
    for path in sorted(DEMO_DIR.glob("*.json")):
        if path.name == "index.json":
            continue
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"[record] skipping unreadable {path.name}")
            continue
        grading = run.get("grading") or {}
        approach = run.get("approach", "agent")
        # Both arms can be recorded on the same instance, so the manifest id -
        # which is what a deep link addresses - carries the approach; the task
        # id underneath stays the benchmark's own.
        run_id = run["task"]["id"] + ("-agentless" if approach == "agentless" else "")
        runs.append(
            {
                "file": path.name,
                "id": run_id,
                "approach": approach,
                "title": run["task"].get("title") or run["task"]["id"],
                "repo": run["task"].get("repo"),
                "difficulty": run["task"].get("difficulty"),
                "resolved": grading.get("resolved") if grading.get("graded") else None,
                "model": run["model"],
                "costUsd": run["costUsd"],
                "turns": run["turns"],
                "durationSeconds": run["durationSeconds"],
                "events": len(run["events"]),
            }
        )

    # Easiest first, then by cost, so the picker reads as a difficulty ramp.
    order = {"<15 min fix": 0, "15 min - 1 hour": 1, "1-4 hours": 2, ">4 hours": 3}
    runs.sort(key=lambda r: (order.get(r["difficulty"], 99), r["costUsd"]))

    (DEMO_DIR / "index.json").write_text(
        json.dumps({"runs": runs}, indent=2), encoding="utf-8"
    )
    print(f"Manifest: {len(runs)} run(s) -> {DEMO_DIR / 'index.json'}")


def scan_for_secrets(payload: str) -> list[str]:
    """Return the patterns that matched, if the serialized recording looks like it leaks."""
    return [p.pattern for p in SECRET_PATTERNS if p.search(payload)]


def to_replay_events(raw: list[dict]) -> list[dict]:
    """Turn absolute event timestamps into clamped per-event playback delays."""
    events = []
    previous = raw[0]["timestamp"] if raw else 0.0
    for event in raw:
        gap_ms = int((event["timestamp"] - previous) * 1000)
        previous = event["timestamp"]
        events.append(
            {
                "type": event["type"],
                "turn": event.get("turn", 0),
                "delayMs": min(max(gap_ms, MIN_DELAY_MS), MAX_DELAY_MS),
                "data": event["data"],
            }
        )
    return events


def resolve_task(args: argparse.Namespace) -> tuple[dict, dict | None]:
    """Return (task, swebench_instance). `task` carries what the agent needs."""
    if args.instance:
        print(f"Loading SWE-bench-lite instance {args.instance} ...")
        instance = load_instance(args.instance)
        difficulty = load_difficulty_labels().get(instance["instance_id"])
        if difficulty:
            print(f"  difficulty (SWE-bench Verified, human annotated): {difficulty}")
        return (
            {
                "kind": "swebench",
                "id": instance["instance_id"],
                "title": headline(instance["problem_statement"]) or instance["instance_id"],
                "repo": instance["repo"],
                "difficulty": difficulty,
                "url": f"https://github.com/{instance['repo']}",
                "repoUrl": f"https://github.com/{instance['repo']}.git",
                "baseCommit": instance["base_commit"],
                "text": instance["problem_statement"],
                # Added plus removed lines. Counting every line of the diff
                # instead would count its headers, hunk markers and context,
                # and then compare that against the agent's real edit count.
                "goldPatchLines": changed_lines(instance["patch"]),
            },
            instance,
        )

    from github_integration.issue_fetcher import fetch_issue

    print(f"Fetching {args.issue} ...")
    issue = fetch_issue(args.issue)
    return (
        {
            "kind": "issue",
            "id": f"{issue.repo_full_name}#{issue.issue_number}",
            "title": issue.issue_title,
            "repo": issue.repo_full_name,
            "difficulty": None,
            "url": args.issue,
            "repoUrl": issue.repo_url,
            "baseCommit": issue.base_commit,
            "text": issue.issue_text,
        },
        None,
    )


def grade_recorded_run(workspace, instance: dict, pytest_args: str) -> dict:
    """
    Grade the agent's patch the way SWE-bench does, and report it either way.

    The test patch is applied only now, after the agent has finished, so the
    agent never saw the tests it is judged on. A failure here is a real result
    and gets recorded as one — the point of running it for real is that the
    outcome is not up to us.
    """
    print("Grading against the instance's own tests ...")
    if not apply_test_patch(workspace, instance):
        return {"graded": False, "reason": "the instance's test patch would not apply"}

    command = build_test_command(instance)
    if pytest_args:
        command = command.replace("-x -q", f"-x -q {pytest_args}")

    result = workspace.run(command, timeout=1800)
    fail_to_pass = json.loads(instance.get("FAIL_TO_PASS") or "[]")
    pass_to_pass = json.loads(instance.get("PASS_TO_PASS") or "[]")

    print(f"  exit {result.exit_code} - {'RESOLVED' if result.success else 'not resolved'}")
    return {
        "graded": True,
        "resolved": result.success,
        "command": command,
        "exitCode": result.exit_code,
        "timedOut": result.timed_out,
        "failToPassCount": len(fail_to_pass),
        "passToPassCount": len(pass_to_pass),
        "testsRun": min(len(fail_to_pass) + len(pass_to_pass), 20),
        "output": result.output[-6000:],
    }


class PipelineRun:
    """
    The pipeline's outcome in the shape the recorder already writes.

    The two arms do not have the same internals - one has turns and a
    conclusion, the other has three phases and a chosen candidate - but the
    recording has to be one format, or the comparison the project exists for
    cannot be made from the files it produces.
    """

    def __init__(self, result, diff: str, duration: float, sampled: int):
        self.diff = diff
        self.turns = 3
        self.input_tokens = result.total_input_tokens
        self.output_tokens = result.total_output_tokens
        self.cost_usd = result.total_cost_usd
        self.duration_seconds = duration
        self.stop_reason = "done" if result.best_candidate else "no_candidate"
        self.conclusion = _pipeline_conclusion(result)
        # How many patches were paid for, and how many were actually run. They
        # differ whenever validation stops at the first candidate that works,
        # and reporting only the second number makes the arm look cheaper than
        # it was.
        self.candidates = sampled
        self.candidates_tested = len(result.all_validations)
        self.error = None


def _pipeline_conclusion(result) -> str:
    from agentless.pipeline import _conclusion

    return _conclusion(result)


def record_agentless(workspace, task: dict, llm, args, raw: list[dict]) -> PipelineRun:
    """Run the three-phase pipeline, capturing its events as they happen."""
    import time

    def capture(event) -> None:
        raw.append(event.to_dict())
        print(f"  [P{event.turn}] {event.type}")

    t0 = time.monotonic()
    result = run_agentless(
        workspace,
        task["text"],
        llm,
        num_samples=args.samples,
        test_command=args.validate_cmd,
        verbose=True,
        on_event=capture,
    )
    duration = time.monotonic() - t0
    # The sampled count lives on the pipeline's own done event; taking it from
    # there keeps one source of truth rather than a second, drifting tally.
    sampled = next(
        (e["data"].get("candidates", 0) for e in reversed(raw) if e["type"] == "done"), 0
    )
    # The winning patch is already written into the workspace by validate(),
    # so the diff is read the same way it is for the agent - from git, not
    # from what the model claimed it changed.
    return PipelineRun(result, workspace.get_diff(), duration, sampled)


def main() -> None:
    args = parse_args()

    provider = PROVIDERS[args.provider]
    model = args.model or provider.models[0].id
    key_env = key_env_for(args.provider)
    api_key = os.getenv(key_env)
    if not api_key:
        sys.exit(f"ERROR: set {key_env} in .env (or the environment) and try again.")

    llm = LLMConfig(provider=args.provider, model=model, api_key=api_key)

    if args.backend == "local":
        print(
            "\n[!] Backend 'local': the agent's shell commands run on THIS machine\n"
            "    with no container around them. Only point it at a repository you\n"
            "    trust. Use --backend docker for anything else.\n"
        )

    task, instance = resolve_task(args)
    print(f"  {task['repoUrl']} @ {task['baseCommit'][:12]} - {task['title']}")

    print(f"Running {args.approach} ({args.provider}/{model}, backend={args.backend}) ...")
    raw: list[dict] = []
    result = None
    setup_result = None
    grading = None

    with create_workspace(task["repoUrl"], task["baseCommit"], backend=args.backend) as workspace:
        if args.setup:
            outcome = workspace.setup(args.setup)
            setup_result = {"command": args.setup, "exitCode": outcome.exit_code}

        if args.approach == "agentless":
            result = record_agentless(workspace, task, llm, args, raw)
        else:
            kwargs = {"max_turns": args.max_turns} if args.max_turns else {}
            generator = run_agent(workspace, task["text"], llm, **kwargs)
            try:
                while True:
                    event = next(generator)
                    raw.append(event.to_dict())
                    print(f"  [T{event.turn}] {event.type}")
            except StopIteration as done:
                result = done.value

        if instance:
            grading = grade_recorded_run(workspace, instance, args.pytest_args)
        elif args.verify:
            print(f"Verifying with: {args.verify}")
            check = workspace.run(args.verify, timeout=1800)
            grading = {
                "graded": True,
                "resolved": check.success,
                "command": args.verify,
                "exitCode": check.exit_code,
                "timedOut": check.timed_out,
                "output": check.output[-6000:],
            }

    if result is None:
        sys.exit("ERROR: the agent produced no result — nothing to record.")

    if not result.diff.strip() and not args.allow_empty:
        sys.exit(
            "ERROR: the agent finished without changing any code, so there is no\n"
            "       patch to show. Re-run (models vary), pick a different task, or\n"
            "       pass --allow-empty to record the attempt anyway."
        )

    task["agentPatchLines"] = changed_lines(result.diff)

    recording = {
        "recordedAt": datetime.now(UTC).isoformat(),
        "backend": args.backend,
        "approach": args.approach,
        "provider": args.provider,
        "model": model,
        "task": task,
        "setup": setup_result,
        "stopReason": result.stop_reason,
        "turns": result.turns,
        "inputTokens": result.input_tokens,
        "outputTokens": result.output_tokens,
        "costUsd": round(result.cost_usd, 6),
        "durationSeconds": round(result.duration_seconds, 1),
        "conclusion": result.conclusion,
        "diff": result.diff,
        "candidates": getattr(result, "candidates", None),
        "candidatesTested": getattr(result, "candidates_tested", None),
        "grading": grading,
        "events": to_replay_events(raw),
    }

    payload = json.dumps(recording, indent=2)

    leaked = scan_for_secrets(payload)
    if leaked:
        sys.exit(
            "ERROR: the recording contains something key-shaped and was NOT written.\n"
            f"       Matched: {leaked}\n"
            "       Check what the agent printed to the shell before retrying."
        )

    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    stem = task["id"].replace("/", "_").replace("#", "-")
    if args.approach == "agentless":
        stem += ".agentless"
    out_path = DEMO_DIR / f"{stem}.json"
    out_path.write_text(payload, encoding="utf-8")
    write_manifest()

    size_kb = len(payload.encode("utf-8")) / 1024
    verdict = "not graded"
    if grading and grading.get("graded"):
        verdict = "RESOLVED" if grading["resolved"] else "not resolved"
    print(
        f"\nWrote {len(recording['events'])} events to {out_path} ({size_kb:.0f} KB)\n"
        f"  {result.turns} turns · ${result.cost_usd:.4f} · "  # noqa: RUF001
        f"{result.input_tokens + result.output_tokens:,} tokens · "
        f"{result.duration_seconds:.0f}s · {changed_lines(result.diff)} lines changed · {verdict}"
    )


if __name__ == "__main__":
    main()
