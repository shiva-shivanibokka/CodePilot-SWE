"""
Choosing one patch from several — the same rule for every arm.

Agentless samples N candidate patches; the agent arm in budget-matched mode
makes N independent attempts. Both hand their candidates here, so the only
difference between the arms is how the candidates were produced.

Autonomous-SWE-Agent's agentless validation (`agentless/validate.py`) ran the
candidates in order and **stopped at the first one that broke nothing**
(`if val.valid: break`). Which patch was submitted therefore depended on
sampling order, a correct patch sampled after a merely harmless one could
never win, and "breaks nothing" was judged by pass/fail *counts* under `-x`,
which reproduced as unable to tell a correct candidate from a breaking one
(docs/MERGE_DECISIONS.md, D9 item 4).

The rule here, following the Agentless paper's own selection (regression
filtering, then majority voting over normalised patches):

1. **Every** candidate is evaluated — no early exit. Each is applied to a
   pristine checkout and the existing tests nearest the changed files are run,
   in full, without `-x`.
2. A candidate **regresses** if any test that passed at baseline does not pass
   with it applied — compared test by test, by id, not by counts.
3. Among candidates that apply and do not regress, the **largest group of
   equivalent patches wins**. Patches are equivalent when the files they
   produce are identical after normalising Python through `ast` (comments,
   blank lines and formatting do not count). Ties go to the smaller patch,
   then the earlier candidate.
4. If every candidate regresses, the same vote runs over all candidates that
   apply, and the result records that it was a fallback.

What this does not do: the Agentless paper also generates *reproduction tests*
from the issue and prefers candidates that pass them. Not implemented; it is
the next step listed in `bench/STUDY_PLAN.md`.
"""

from __future__ import annotations

import ast
import posixpath
from collections import defaultdict
from dataclasses import dataclass, field

from codepilot.bench import checkout, testlog
from codepilot.bench.grading import filter_source_diff, is_test_path

REGRESSION_TIMEOUT = 1200


@dataclass
class Candidate:
    index: int
    diff: str
    origin: str  # "agent attempt 2", "agentless sample 4 (calc.py)"


@dataclass
class Evaluation:
    candidate: Candidate
    applied: bool
    regressions: list[str] = field(default_factory=list)
    key: tuple = ()
    detail: str = ""

    @property
    def clean(self) -> bool:
        return self.applied and not self.regressions


@dataclass
class Selection:
    chosen: Evaluation | None
    basis: str
    evaluations: list[Evaluation]
    regression_command: str = ""
    votes: int = 0


def nearest_test_dirs(files: list[str], all_files: list[str]) -> list[str]:
    """For each changed source file, the closest `tests` directory above it.

    From Autonomous-SWE-Agent's `tests_near`: the tests beside the changed code
    are the ones that would catch a regression in it, and they run in seconds
    where a whole suite (sympy's) runs for most of an hour.
    """
    dirs = {posixpath.dirname(f) for f in all_files if is_test_path(f)}
    found: list[str] = []
    def holds_tests(candidate: str) -> bool:
        return any(d == candidate or d.startswith(candidate + "/") for d in dirs)

    for path in files:
        directory = posixpath.dirname(path)
        while True:
            names = [posixpath.join(directory, n) if directory else n for n in ("tests", "test", "testing")]
            hit = next((c for c in names if holds_tests(c)), None)
            if hit:
                found.append(hit)
                break
            if not directory:
                break
            directory = posixpath.dirname(directory)
    return list(dict.fromkeys(found))


def regressions_between(baseline: dict[str, str], after: dict[str, str]) -> list[str]:
    """Tests that passed before the patch and do not pass with it.

    Compared by test id. A test that was already failing does not count
    against a patch; a test that stops being collected (an import the patch
    broke) does, because it is missing from `after`.
    """
    return sorted(
        t for t, s in baseline.items()
        if s in testlog.PASSING and after.get(t) not in testlog.PASSING
    )


def regression_command(test_dirs: list[str]) -> str:
    targets = " ".join(f'"{d}"' for d in test_dirs)
    return f"python -m pytest -rA -p no:cacheprovider --tb=no -q {targets}".rstrip()


def normalised_key(root, diff: str) -> tuple:
    """What the patch makes the changed files *be*, ignoring formatting."""
    key = []
    for path in sorted(checkout.changed_files(diff)):
        target = root / path
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            key.append((path, None))
            continue
        if path.endswith(".py"):
            try:
                text = ast.unparse(ast.parse(text))
            except (SyntaxError, ValueError):
                text = "\n".join(line.rstrip() for line in text.splitlines() if line.strip())
        key.append((path, text))
    return tuple(key)


async def select(env, candidates: list[Candidate], *, repo: str = "") -> Selection:
    """Evaluate every candidate on a pristine tree and choose one.

    `env` is a BenchEnv. The tree is left restored to its baseline.
    """
    usable = [c for c in candidates if c.diff.strip()]
    if not usable:
        return Selection(None, "no candidate changed anything", [])

    all_files = env_files(env)
    sources = sorted({f for c in usable for f in checkout.changed_files(filter_source_diff(c.diff)[0])})
    test_dirs = nearest_test_dirs(sources, all_files) if repo != "django/django" else []
    command = regression_command(test_dirs) if test_dirs else ""

    baseline: dict[str, str] = {}
    if command:
        env.restore()
        baseline = testlog.parse_pytest((await env.run(command, timeout=REGRESSION_TIMEOUT)).combined)

    evaluations = []
    for cand in usable:
        env.restore()
        kept, _ = filter_source_diff(cand.diff)
        ok, detail = env.apply(kept) if kept.strip() else (False, "no source change")
        ev = Evaluation(cand, ok, detail=detail)
        if ok:
            ev.key = normalised_key(env.root, kept)
            if command:
                after = testlog.parse_pytest((await env.run(command, timeout=REGRESSION_TIMEOUT)).combined)
                ev.regressions = regressions_between(baseline, after)
        evaluations.append(ev)
    env.restore()

    clean = [e for e in evaluations if e.clean]
    applied = [e for e in evaluations if e.applied]
    if clean:
        pool, basis = clean, "majority vote among regression-free candidates"
    elif applied:
        pool, basis = applied, "fallback: every candidate regressed; majority vote among all that apply"
    else:
        return Selection(None, "no candidate applies to a clean checkout", evaluations, command)
    if not command:
        basis += " (no regression tests found near the changed files)"

    groups: dict[tuple, list[Evaluation]] = defaultdict(list)
    for e in pool:
        groups[e.key].append(e)
    best = min(
        groups.values(),
        key=lambda g: (
            -len(g),
            len(g[0].regressions),
            checkout.changed_lines(g[0].candidate.diff),
            g[0].candidate.index,
        ),
    )
    return Selection(best[0], basis, evaluations, command, votes=len(best))


def env_files(env) -> list[str]:
    from codepilot.workspace import Workspace

    return Workspace(root=env.root).list_files()
