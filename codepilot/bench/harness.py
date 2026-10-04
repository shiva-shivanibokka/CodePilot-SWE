"""
Run the arms on one SWE-bench instance and grade what they submit.

One environment per instance, shared by every arm: the checkout is cloned and
set up once, and restored to its baseline before each attempt, sample and
grade. Two arms therefore see byte-identical trees and the same installed
environment, and differ only in how they produce a patch.

Arms:

* `agent` — CodePilot's loop (`codepilot/agent/loop.py`), N independent
  attempts. N = 1 submits the attempt's diff; N > 1 goes through
  `selection.select`, exactly as agentless's samples do.
* `agentless` — localise, sample N patches, `selection.select`.

`--attempts N` sets N for both, which is the budget-matched mode: N agent
attempts against N agentless samples. Budget-matched means matched in
*attempts*, not in tokens or dollars — the agent's attempts are far more
expensive, and the results report cost per arm so the reader can see by how
much.

A run the provider refused to serve (rate limit after retries, outage) is
recorded with `infra_error` and excluded from scoring, as CodePilot's own eval
always did: an outage is not a result.
"""

from __future__ import annotations

import random
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from codepilot.agent.loop import AgentLoop
from codepilot.bench import checkout, swebench
from codepilot.bench.agentless import run_agentless
from codepilot.bench.environment import BenchEnv, fingerprint_diff, swebench_image
from codepilot.bench.prompts import AGENT_ARM, SHARED_BASE, issue_message
from codepilot.bench.selection import Candidate, select
from codepilot.context import Conversation
from codepilot.events import EventStream, EventType
from codepilot.llm import AbortRun, LLMClient, LLMError, Usage
from codepilot.permissions import Budget, PermissionGate
from codepilot.tools import ToolContext
from codepilot.workspace import Workspace

ARMS = ("agent", "agentless")

#: Provider-side failures: the agent never got (or lost) its turn.
INFRASTRUCTURE = (
    "RateLimitError", "ServiceUnavailableError", "InternalServerError",
    "APIConnectionError", "Timeout", "overloaded", "Request too large",
    "PerDay", "per day", "quota",
)


def is_infrastructure(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}"
    return any(marker in text for marker in INFRASTRUCTURE)


@dataclass
class ArmConfig:
    model: str
    attempts: int = 1
    max_usd: float = 1.00  # per attempt, at list price
    max_turns: int = 40  # model calls per attempt
    max_tokens: int = 800_000  # per attempt
    compact_at: int = 100_000
    tools: list[str] | None = None  # None = CodePilot's whole tool set
    #: Run seed. Agent attempt k sends seed*1000+k; agentless sample k sends
    #: seed*1000+500+k; localisation seed*1000+999. None sends no seed.
    seed: int | None = None
    #: Output tokens per agent call. Lower it for a small context window.
    max_output_tokens: int = 8192


@dataclass
class Spend:
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    calls: int = 0
    unpriced_calls: int = 0

    def add(self, usage: Usage, cost: float | None) -> None:
        self.usage = self.usage + usage
        self.calls += 1
        if cost is None:
            self.unpriced_calls += 1
        else:
            self.cost_usd += cost


@dataclass
class InstanceResult:
    instance_id: str
    repo: str
    arm: str
    model: str
    attempts: int
    resolved: bool
    submitted: bool
    grade: dict
    selection_basis: str
    selected: str
    votes: int
    candidates: list[dict]
    cost_usd: float
    unpriced_calls: int
    model_calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    wall_seconds: float
    stopped_by: list[str]
    changed_lines: int
    diff: str
    backend: str
    image: str
    setup: list[dict]
    error: str = ""
    infra_error: bool = False
    notes: list[str] = field(default_factory=list)
    #: Changes to what `restore` does not reset (kept ignored files, installed
    #: packages) between the environment's creation and this arm's grading.
    #: Non-empty means this row ran or was graded in a changed environment (D35).
    contamination: list[str] = field(default_factory=list)
    #: The order the arms ran in on this instance (randomised, D35).
    arm_order: list[str] = field(default_factory=list)
    #: What the arm did, compactly: the model's words, each tool call and the
    #: first line of its result, how it stopped. Without it a row can say an
    #: agent "finished" after one call and nothing about why.
    transcript: list[dict] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# The agent arm
# ---------------------------------------------------------------------------


def cache_prefix_report(model: str) -> dict:
    """Whether each arm's fixed prefix can be cached on `model` (D34).

    Estimates are LiteLLM's token counter (a generic tokenizer), so they are
    approximate; the minimum is Anthropic's published one.
    """
    from codepilot.bench.prompts import LOCALIZE_ARM, REPAIR_ARM
    from codepilot.llm import _litellm, cache_minimum, to_openai_tools
    from codepilot.tools import schemas

    minimum = cache_minimum(model)
    report: dict = {"model": model, "minimum_cacheable_tokens": minimum, "prefixes": {}}
    for name, text, tools in (
        ("agent", SHARED_BASE + "\n" + AGENT_ARM, to_openai_tools(schemas())),
        ("agentless_localize", SHARED_BASE + "\n" + LOCALIZE_ARM, None),
        ("agentless_repair", SHARED_BASE + "\n" + REPAIR_ARM, None),
    ):
        try:
            tokens = _litellm().token_counter(
                model=model, messages=[{"role": "system", "content": text}], tools=tools)
        except Exception:  # noqa: BLE001 - estimate unavailable
            tokens = None
        report["prefixes"][name] = {
            "estimated_tokens": tokens,
            "caches_on_its_own": None if (minimum is None or tokens is None) else tokens >= minimum,
        }
    return report


def tag_of(events: EventStream) -> str:
    return events.session_id


def transcript_of(events: EventStream, limit: int = 300) -> list[dict]:
    """The agent's events as a short, committed-to-disk-sized record."""
    out: list[dict] = []
    for e in events.events:
        if e.type is EventType.ASSISTANT_TEXT:
            out.append({"kind": "text", "text": e.message[:limit]})
        elif e.type is EventType.TOOL_CALL:
            out.append({"kind": "tool_call", "tool": e.data.get("tool"),
                        "args": str(e.data.get("arguments"))[:limit]})
        elif e.type is EventType.TOOL_RESULT:
            out.append({"kind": "tool_result", "tool": e.data.get("tool"),
                        "error": bool(e.data.get("is_error")), "text": e.message[:limit]})
        elif e.type in (EventType.DONE, EventType.ERROR, EventType.BUDGET):
            out.append({"kind": e.type.value if e.type is not EventType.DONE else "done",
                        "text": e.message[:limit]})
    return out


async def agent_attempt(env: BenchEnv, client, cfg: ArmConfig, issue: str, attempt: int,
                        spend: Spend, events: EventStream) -> tuple[str, str, BaseException | None]:
    """One run of CodePilot's loop on a pristine tree.

    Returns (diff, stopped_by, error). An error does not discard the attempt:
    whatever the agent had changed when it stopped is still its diff, and the
    error travels with it so the result can say why it stopped.
    """
    env.restore()
    ctx = ToolContext(
        workspace=Workspace(root=env.root, session_id=f"bench-{attempt}"),
        sandbox=env.sandbox,
        permissions=PermissionGate(auto_approve=True),
        events=events,
    )
    convo = Conversation(system_prompt=SHARED_BASE + "\n" + AGENT_ARM, compact_at=cfg.compact_at)
    budget = Budget(max_usd=cfg.max_usd, max_turns=cfg.max_turns, max_tokens=cfg.max_tokens)
    loop = AgentLoop(
        client, ctx, convo, budget,
        tool_names=cfg.tools, effort=None, model=cfg.model,
        temperature=0.2 if attempt == 0 else 1.0,
        seed=None if cfg.seed is None else cfg.seed * 1000 + attempt,
        max_tokens=cfg.max_output_tokens,
        cache_tag=f"{tag_of(events)}:attempt-{attempt}",
    )
    before = len(events.events)
    stopped_by, error = "error", None
    try:
        stopped_by = (await loop.run(issue_message(issue))).stopped_by
    except AbortRun:
        raise
    except Exception as exc:  # noqa: BLE001 - reported with the attempt
        error = exc
    finally:
        for e in events.events[before:]:
            if e.type is EventType.COST:
                spend.add(
                    Usage(
                        input_tokens=int(e.data.get("input_tokens") or 0),
                        output_tokens=int(e.data.get("output_tokens") or 0),
                        cache_read_tokens=int(e.data.get("cache_read") or 0),
                        cache_write_tokens=int(e.data.get("cache_write") or 0),
                    ),
                    e.data.get("cost_usd"),
                )
    return env.diff(), stopped_by, error


# ---------------------------------------------------------------------------
# One instance, every arm
# ---------------------------------------------------------------------------


async def run_instance(
    instance: dict,
    arms: list[str],
    cfg: ArmConfig,
    *,
    backend: str = "local",
    setup: str | None = None,
    image: str | None = None,
    python: str | None = None,
    client: LLMClient | None = None,
    on_result=None,
    env_options: dict | None = None,
) -> list[InstanceResult]:
    """Every arm on one instance, in one environment. `env_options` goes to
    `BenchEnv.create` (tests use it to skip the virtualenv and the install)."""
    repo_url = instance.get("repo_url") or f"https://github.com/{instance['repo']}.git"
    if backend == "docker" and image == "official":
        image = swebench_image(instance["instance_id"])
    client = client or LLMClient(model=cfg.model)
    results: list[InstanceResult] = []
    env = await BenchEnv.create(
        repo_url, instance["base_commit"], backend=backend, setup=setup, image=image,
        python=python, task_id=instance["instance_id"], **(env_options or {}),
    )
    try:
        # Arm order is randomised per instance, deterministically from the run
        # seed and the instance id, so neither arm always runs on a freshly set
        # up environment while the other inherits the first one's side effects.
        order = list(arms)
        random.Random(f"{cfg.seed}:{instance['instance_id']}").shuffle(order)
        baseline = await env.fingerprint()
        for arm in order:
            result = await _run_arm(env, instance, arm, cfg, client)
            result.arm_order = order
            result.contamination = fingerprint_diff(baseline, await env.fingerprint())
            if result.contamination:
                result.notes.append(
                    "environment changed since setup (kept files or installed packages): "
                    "this arm ran or was graded in it"
                )
            results.append(result)
            if on_result:
                on_result(result)
    finally:
        await env.close()
    return results


async def _run_arm(env: BenchEnv, instance: dict, arm: str, cfg: ArmConfig, client) -> InstanceResult:
    started = time.monotonic()
    spend = Spend()
    tag = f"{instance['instance_id']}:{arm}"
    if hasattr(client, "tag"):
        # Every call this arm makes is labelled, in memory and in the ledger,
        # so its spend is known even if a later stage raises (D27).
        client.tag = tag
    events = EventStream(session_id=f"{instance['instance_id']}:{arm}")
    candidates: list[Candidate] = []
    stopped: list[str] = []
    notes: list[str] = []
    error, infra = "", False
    issue = instance.get("problem_statement", "")
    agentless_transcript: list[dict] = []

    try:
        if arm == "agent":
            for attempt in range(cfg.attempts):
                diff, why, exc = await agent_attempt(env, client, cfg, issue, attempt, spend, events)
                stopped.append(why)
                candidates.append(Candidate(attempt, diff, f"agent attempt {attempt + 1}"))
                if exc is not None:
                    error = f"attempt {attempt + 1}: {type(exc).__name__}: {exc}"[:2000]
                    infra = infra or is_infrastructure(exc)
                    if infra:
                        break
        elif arm == "agentless":
            run = await run_agentless(env, client, cfg.model, issue, cfg.attempts, seed=cfg.seed)
            spend.usage = spend.usage + run.usage
            spend.cost_usd += run.cost_usd
            spend.calls += run.calls
            spend.unpriced_calls += run.unpriced_calls
            candidates = run.candidates
            notes += run.notes
            agentless_transcript = [
                {"kind": "localize", "files": run.localization.suspect_files[:5],
                 "locations": run.localization.suspect_locations[:3]},
                *[
                    {"kind": "sample", "index": s.index, "path": s.path,
                     "usable": s.patched is not None, "text": s.note[:300]}
                    for s in run.repair.samples
                ],
            ]
            stopped.append(f"{len(run.candidates)} of {cfg.attempts} samples usable")
        else:
            raise ValueError(f"unknown arm {arm!r}")
    except AbortRun:
        # The spend cap, or a request the provider rejected: the run stops
        # here and nothing about this arm is scored (D28, D29).
        raise
    except (LLMError, Exception) as exc:  # noqa: BLE001 - one bad arm must not end the sweep
        error = f"{type(exc).__name__}: {exc}"[:2000]
        infra = is_infrastructure(exc)
        stopped.append("error")

    if len(candidates) == 1:
        chosen_diff = candidates[0].diff
        basis, selected, votes, cand_info = "single attempt, no selection", candidates[0].origin, 1, []
        if not chosen_diff.strip():
            basis = "single attempt produced no change"
    elif candidates:
        sel = await select(env, candidates, repo=instance.get("repo", ""))
        chosen_diff = sel.chosen.candidate.diff if sel.chosen else ""
        basis = sel.basis + (f" — {sel.regression_command}" if sel.regression_command else "")
        selected = sel.chosen.candidate.origin if sel.chosen else ""
        votes = sel.votes
        cand_info = [
            {
                "origin": e.candidate.origin,
                "applied": e.applied,
                "regressions": e.regressions[:20],
                "n_regressions": len(e.regressions),
                "changed_files": checkout.changed_files(e.candidate.diff),
                "changed_lines": checkout.changed_lines(e.candidate.diff),
            }
            for e in sel.evaluations
        ]
    else:
        chosen_diff, basis, selected, votes, cand_info = "", "no candidates", "", 0, []

    if hasattr(client, "spent"):
        recorded = client.spent(tag)
        spend.usage, spend.cost_usd = recorded.usage, recorded.cost_usd
        spend.calls, spend.unpriced_calls = recorded.calls, recorded.unpriced_calls

    report = await swebench.grade(env, instance, chosen_diff)
    env.restore()
    return InstanceResult(
        instance_id=instance["instance_id"],
        repo=instance.get("repo", ""),
        arm=arm,
        model=cfg.model,
        attempts=cfg.attempts,
        resolved=report.resolved and not infra,
        submitted=bool(chosen_diff.strip()),
        grade=report.to_dict(),
        selection_basis=basis,
        selected=selected,
        votes=votes,
        candidates=cand_info,
        cost_usd=round(spend.cost_usd, 6),
        unpriced_calls=spend.unpriced_calls,
        model_calls=spend.calls,
        input_tokens=spend.usage.input_tokens,
        output_tokens=spend.usage.output_tokens,
        cache_read_tokens=spend.usage.cache_read_tokens,
        cache_write_tokens=spend.usage.cache_write_tokens,
        wall_seconds=round(time.monotonic() - started, 1),
        stopped_by=stopped,
        changed_lines=checkout.changed_lines(chosen_diff),
        diff=chosen_diff,
        backend=env.backend,
        image=env.image,
        setup=[asdict(s) for s in env.setup],
        error=error,
        infra_error=infra,
        notes=notes,
        transcript=transcript_of(events) + agentless_transcript,
    )
