"""
SWE-bench Lite: the dataset, the graded test command, and grading.

From Autonomous-SWE-Agent's `eval/harness.py`. The dataset loader is kept as
it was. The grading is rewritten, because three properties of the original
could each score a wrong patch as resolved or a right one as failed — all
three reproduced before the change (docs/MERGE_DECISIONS.md, D9):

* **A 20-test cap** (`MAX_GRADED_TESTS`): instances with more FAIL_TO_PASS +
  PASS_TO_PASS ids than that were graded on the first twenty only.
* **`-k` substring selection for bare test names**: `-k "test_issue_1"`
  also selects `test_issue_10`, so a required test that does not exist could
  be "passed" by another one.
* **Exit-code grading with `-x`**: the run stopped at the first failure and
  only its exit status was read.

Now: every test file the required ids live in (and every file the test patch
touches) is run in full, with `-rA -v` and no `-x`; the log is parsed into
per-test outcomes (`testlog.py`); and every FAIL_TO_PASS and PASS_TO_PASS id is
looked up **exactly**. Resolved means every one of them passed — the official
SWE-bench criterion. A required id that never ran counts as a failure.

Grading happens on a pristine tree: the checkout is restored to its baseline,
only the agent's filtered source diff is applied (`grading.py`), then the test
patch. The official harness re-runs in its own per-instance images; this is
the same criterion in this repository's environment, and results say which
backend and setup produced them.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath

from codepilot.bench import testlog
from codepilot.bench.grading import filter_source_diff

HF_ROWS_URL = (
    "https://datasets-server.huggingface.co/rows"
    "?dataset=princeton-nlp%2FSWE-bench_Lite&config=default&split=test"
    "&offset={offset}&length={length}"
)
SWEBENCH_LITE_SIZE = 300
GRADE_TIMEOUT = 1800


def _load_via_http(limit: int | None = None) -> list[dict]:
    """
    Read SWE-bench-lite straight from the dataset's public rows API.

    The `swebench` package pulls the whole `datasets` stack in order to hand
    back 300 dictionaries. This is the same data over plain HTTP, so listing or
    recording a single instance does not require a heavyweight install.
    """
    import json as _json
    import urllib.request

    wanted = min(limit or SWEBENCH_LITE_SIZE, SWEBENCH_LITE_SIZE)
    rows: list[dict] = []
    for offset in range(0, wanted, 100):
        url = HF_ROWS_URL.format(offset=offset, length=min(100, wanted - offset))
        with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - fixed https URL
            rows += [row["row"] for row in _json.load(response)["rows"]]
    return rows


def load_swebench_lite(limit: int | None = None, *, live: bool = False) -> list[dict]:
    """
    SWE-bench Lite instances, in the dataset's own order.

    By default the frozen copy committed in `codepilot/bench/data/` (checked by
    hash, `instances.py`, D37), so a study draws from exactly the rows it
    records. `live=True` fetches from the `swebench` package or, failing that,
    the dataset's HTTP API, as Autonomous-SWE-Agent did.
    """
    if not live:
        from codepilot.bench.instances import load_all

        instances = load_all()
        return instances[:limit] if limit else instances
    try:
        from swebench.harness.utils import load_swebench_dataset

        instances = list(load_swebench_dataset("princeton-nlp/SWE-bench_Lite", split="test"))
    except ImportError:
        instances = _load_via_http(None if limit is None else SWEBENCH_LITE_SIZE)
    return instances[:limit] if limit else instances


def load_instances(ids: list[str]) -> list[dict]:
    """Specific instances, in the order asked for."""
    wanted = set(ids)
    found = {i["instance_id"]: i for i in load_swebench_lite() if i["instance_id"] in wanted}
    missing = [i for i in ids if i not in found]
    if missing:
        raise KeyError(f"not in SWE-bench Lite: {missing}")
    return [found[i] for i in ids]


VERIFIED_ROWS_URL = (
    "https://datasets-server.huggingface.co/rows"
    "?dataset=princeton-nlp%2FSWE-bench_Verified&config=default&split=test"
    "&offset={offset}&length={length}"
)
SWEBENCH_VERIFIED_SIZE = 500


def load_difficulty_labels() -> dict[str, str]:
    """
    Human difficulty estimates for SWE-bench instances.

    SWE-bench Verified carries a `difficulty` field — how long the annotators
    judged each issue would take an engineer ("<15 min fix", "15 min - 1 hour",
    "1-4 hours", ">4 hours"). Roughly a third of the Lite set also appears in
    Verified and so has a label; unlabelled instances stay unlabelled rather
    than guessed at. The study plan uses it to stratify results.

    Returns {} rather than raising if the dataset cannot be reached.
    """
    import json as _json
    import urllib.request

    labels: dict[str, str] = {}
    try:
        for offset in range(0, SWEBENCH_VERIFIED_SIZE, 100):
            url = VERIFIED_ROWS_URL.format(offset=offset, length=100)
            with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310
                for entry in _json.load(response)["rows"]:
                    row = entry["row"]
                    if row.get("difficulty"):
                        labels[row["instance_id"]] = row["difficulty"]
    except Exception as exc:  # noqa: BLE001 - any network failure is non-fatal
        print(f"[bench] could not load difficulty labels ({exc}); continuing without them")
    return labels


# ---------------------------------------------------------------------------
# What to run
# ---------------------------------------------------------------------------


def test_ids(instance: dict, key: str) -> list[str]:
    """One of the instance's test-id lists, which arrive as JSON strings."""
    value = instance.get(key, "[]")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return list(value or [])


def patched_files(patch: str) -> list[str]:
    """Every file a unified diff touches (new and modified)."""
    return list(dict.fromkeys(re.findall(r"^\+\+\+ b/(.+?)\s*$", patch or "", re.MULTILINE)))


@dataclass
class TestSpec:
    kind: str  # "pytest" | "django"
    command: str
    targets: list[str]


def build_test_spec(instance: dict) -> TestSpec:
    """The command that runs every required test, and how to read its log.

    pytest repositories: every file named by a node id plus every Python file
    the test patch touches, run whole. Bare names (sympy) live in the patched
    files, so the patched files are what run. No `-k`, no `-x`, no cap.

    Django: its own runner, at verbosity 2, on the test modules the test patch
    touches — as SWE-bench runs it.
    """
    ids = test_ids(instance, "FAIL_TO_PASS") + test_ids(instance, "PASS_TO_PASS")
    touched = [p for p in patched_files(instance.get("test_patch", "")) if p.endswith(".py")]
    if instance.get("repo") == "django/django":
        labels = []
        for path in touched:
            p = PurePosixPath(path)
            if p.parts and p.parts[0] == "tests":
                labels.append(".".join(p.with_suffix("").parts[1:]))
        for test_id in ids:
            m = re.match(r"^\w+ \(([\w.]+)\)$", test_id)
            if m and not labels:
                labels.append(m.group(1).rsplit(".", 1)[0])
        labels = list(dict.fromkeys(labels))
        command = (
            "python tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1 "
            + " ".join(labels)
        )
        return TestSpec("django", command, labels)

    files = [t.split("::", 1)[0] for t in ids if "::" in t]
    targets = list(dict.fromkeys(files + touched))
    quoted = " ".join(f'"{t}"' for t in targets)
    command = f"python -m pytest -rA -v -p no:cacheprovider --tb=short {quoted}".rstrip()
    return TestSpec("pytest", command, targets)


def parse_statuses(spec: TestSpec, log: str) -> dict[str, str]:
    return testlog.parse_django(log) if spec.kind == "django" else testlog.parse_pytest(log)


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


@dataclass
class GradeReport:
    resolved: bool
    applied: bool
    f2p_passed: int = 0
    f2p_total: int = 0
    p2p_passed: int = 0
    p2p_total: int = 0
    #: Required ids that did not pass, with what happened ("missing" = never ran).
    failures: dict[str, str] = field(default_factory=dict)
    #: Files in the agent's diff kept out of grading, and why.
    dropped: dict[str, str] = field(default_factory=dict)
    command: str = ""
    exit_code: int | None = None
    detail: str = ""
    log_tail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def judge(instance: dict, statuses: dict[str, str]) -> tuple[bool, dict, dict[str, str]]:
    """Resolved iff every FAIL_TO_PASS and PASS_TO_PASS id passed."""
    counts: dict[str, list[int]] = {}
    failures: dict[str, str] = {}
    for key in ("FAIL_TO_PASS", "PASS_TO_PASS"):
        ids = test_ids(instance, key)
        ok = 0
        for test_id in ids:
            status = testlog.lookup(statuses, test_id)
            if status in testlog.PASSING:
                ok += 1
            else:
                failures[test_id] = status or "missing"
        counts[key] = [ok, len(ids)]
    resolved = not failures and counts["FAIL_TO_PASS"][1] > 0
    return resolved, counts, failures


async def grade(env, instance: dict, diff: str, *, run_if_empty: bool = False) -> GradeReport:
    """Grade an agent's diff on a pristine checkout. Leaves the tree graded.

    `env` is a `BenchEnv`. The agent's working tree is discarded first: the
    only thing that carries over is the filtered source diff.

    An empty diff is unresolved without running anything, unless
    `run_if_empty` — the harness check's `empty` arm, which must see the
    FAIL_TO_PASS tests actually fail on the untouched checkout.
    """
    test_patch = instance.get("test_patch") or ""
    kept, dropped = filter_source_diff(diff, set(patched_files(test_patch)))
    env.restore()
    report = GradeReport(resolved=False, applied=False, dropped=dropped)
    if not kept.strip() and not run_if_empty:
        report.detail = "no source changes to grade" + (
            f" ({len(dropped)} file(s) dropped)" if dropped else ""
        )
        return report
    ok, detail = env.apply(kept) if kept.strip() else (True, "nothing to apply")
    if not ok:
        report.detail = f"the agent's patch does not apply to a clean checkout: {detail}"
        return report
    ok, detail = env.apply(test_patch)
    if not ok:
        report.detail = f"the test patch does not apply on top of the agent's: {detail}"
        return report
    report.applied = True

    spec = build_test_spec(instance)
    result = await env.run(spec.command, timeout=GRADE_TIMEOUT)
    statuses = parse_statuses(spec, result.combined)
    resolved, counts, failures = judge(instance, statuses)
    report.resolved = resolved
    report.f2p_passed, report.f2p_total = counts["FAIL_TO_PASS"]
    report.p2p_passed, report.p2p_total = counts["PASS_TO_PASS"]
    report.failures = failures
    report.command = spec.command
    report.exit_code = result.exit_code
    report.log_tail = result.combined[-3000:]
    if result.timed_out:
        report.detail = "the graded test run timed out"
    return report
