"""The GitHub issue -> fix -> PR integration, offline.

Reproductions (docs/MERGE_DECISIONS.md, D17), each failing on
Autonomous-SWE-Agent's `pr_creator._commit_and_push` before the fix:

* it built `git commit -m "{message}"` and ran it with `shell=True`, so an
  issue title containing `$(...)` executed on the host;
* it pushed to `https://{token}@github.com/...`, putting the token in the
  command line (visible in the process list) and in git's error output.
"""

from __future__ import annotations

import subprocess

import pytest

from codepilot.integrations.github import pr_creator
from codepilot.integrations.github.issue_fetcher import IssueData, parse_github_url


def _issue(title: str) -> IssueData:
    return IssueData(
        issue_url="https://github.com/o/r/issues/7", repo_url="https://github.com/o/r.git",
        repo_full_name="o/r", issue_number=7, issue_title=title, issue_body="",
        base_commit="HEAD", branch="main", labels=[],
    )


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"],
                   cwd=tmp_path, check=True)
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    return tmp_path


def test_an_issue_title_cannot_run_commands(repo, monkeypatch):
    pushed = []
    monkeypatch.setattr(pr_creator, "_push", lambda *a, **kw: pushed.append(a))
    title = 'Fix "it" $(echo PWNED > pwned.txt) `touch pwned2.txt` " & echo PWNED > pwned3.txt & echo "'
    files = pr_creator._commit_and_push(str(repo), "fix-7", _issue(title), "conclusion", "tok")
    assert not (repo / "pwned.txt").exists()
    assert not (repo / "pwned2.txt").exists()
    assert not (repo / "pwned3.txt").exists()
    assert files == ["a.py"]
    message = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=repo,
                             capture_output=True, text=True).stdout
    assert title in message, "the title must arrive verbatim, not shell-interpreted"


def test_the_token_is_never_on_a_command_line(repo, monkeypatch):
    seen = []
    real_run = subprocess.run

    def spy(args, *a, **kw):
        seen.append((list(args) if not isinstance(args, str) else [args], kw.get("env") or {}))
        if isinstance(args, list) and "push" in args:
            return subprocess.CompletedProcess(args, 0, "", "")
        return real_run(args, *a, **kw)

    monkeypatch.setattr(pr_creator.subprocess, "run", spy)
    pr_creator._commit_and_push(str(repo), "fix-7", _issue("t"), "c", "ghp_SECRETTOKEN")
    for argv, _ in seen:
        assert not any("ghp_SECRETTOKEN" in part for part in argv), argv


def test_the_pr_body_does_not_claim_what_was_not_measured():
    body = pr_creator._build_pr_body(_issue("t"), "diff", "conclusion", ["a.py"])
    assert "production" not in body.lower()
    assert "git checkout codepilot/fix-issue-7" in body, "the test instructions must name the branch"


# Ported from B's tests/test_harness.py::TestGithubUrlParser
def test_full_url():
    assert parse_github_url("https://github.com/scikit-learn/scikit-learn/issues/12462") == (
        "scikit-learn/scikit-learn", 12462)


def test_short_format():
    assert parse_github_url("owner/repo#42") == ("owner/repo", 42)


def test_invalid_url_raises():
    with pytest.raises(ValueError):
        parse_github_url("not-a-url")


# ------------------------------------------------- issue -> loop -> PR, offline


async def test_an_issue_is_solved_by_the_merged_loop_and_a_pr_only_on_request(tmp_path):
    from codepilot.bench.harness import ArmConfig
    from codepilot.integrations.github.solve import solve
    from tests.benchfixture import make_task
    from tests.test_bench_e2e import AGENT_FIXES, ScriptedModel

    instance, _ = make_task(tmp_path)
    issue = IssueData(
        issue_url="x", repo_url=instance["repo_url"], repo_full_name="local/calc",
        issue_number=1, issue_title="add subtracts", issue_body="calc.add is wrong",
        base_commit=instance["base_commit"], branch="main", labels=[],
    )
    opened = []
    opts = {"install": False, "venv": False}
    cfg = ArmConfig(model="scripted", max_turns=20)

    result = await solve("x", cfg, client=ScriptedModel([AGENT_FIXES], []), fetch=lambda u: issue,
                         create=lambda *a, **kw: opened.append(a), env_options=opts)
    assert "return a + b" in result["diff"] and result["stopped_by"] == "finished"
    assert opened == [], "a PR was opened without --open-pr"

    class PR:
        pr_url = "https://example.invalid/pr/1"

    result = await solve("x", cfg, client=ScriptedModel([AGENT_FIXES], []), fetch=lambda u: issue,
                         create=lambda *a, **kw: opened.append(kw) or PR(), open_pr=True,
                         env_options=opts)
    assert result["pr"] == PR.pr_url
    assert opened and opened[0]["repo_local_path"]


def test_the_event_stream_drives_the_prometheus_metrics():
    from prometheus_client import CollectorRegistry

    from codepilot.events import EventStream, EventType
    from codepilot.integrations.observability.events import attach
    from codepilot.integrations.observability.metrics import AgentMetrics

    registry = CollectorRegistry()
    m = AgentMetrics(registry=registry)
    stream = EventStream(session_id="t")
    attach(stream, approach="agent", registry=m)
    stream.emit(EventType.TURN_START, "go")
    stream.emit(EventType.TOOL_RESULT, "ok", tool="read_file", is_error=False)
    stream.emit(EventType.COST, "c", cost_usd=0.01, input_tokens=100, output_tokens=10)
    stream.emit(EventType.DONE, "done", stopped_by="finished")
    get = registry.get_sample_value
    assert get("swe_agent_tasks_resolved_total", {"approach": "agent"}) == 1
    assert get("swe_agent_tool_calls_total", {"tool_name": "read_file"}) == 1
    assert get("swe_agent_input_tokens_total", {"approach": "agent"}) == 100
