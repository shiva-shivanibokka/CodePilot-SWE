"""The frozen dataset and the optional official grader (D37)."""

from __future__ import annotations

import json
import re
import urllib.request

import pytest

from codepilot.bench import instances, swebench


def test_the_frozen_dataset_is_the_recorded_one():
    rows = instances.load_all()
    assert len(rows) == 300
    assert len({r["instance_id"] for r in rows}) == 300
    assert {"FAIL_TO_PASS", "PASS_TO_PASS", "test_patch", "image", "eval_script"} <= set(rows[0])


def test_loading_needs_no_network(monkeypatch):
    def refuse(*a, **kw):
        raise AssertionError("the frozen dataset must not touch the network")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    assert [r["instance_id"] for r in swebench.load_instances(["pallets__flask-4992"])] == ["pallets__flask-4992"]


def test_a_seed_always_draws_the_same_instances():
    first = [r["instance_id"] for r in instances.sample(20, seed=0)]
    assert first == [r["instance_id"] for r in instances.sample(20, seed=0)]
    assert first != [r["instance_id"] for r in instances.sample(20, seed=1)]
    assert len(set(first)) == 20


def test_a_tampered_dataset_is_refused(tmp_path, monkeypatch):
    import gzip

    bad = tmp_path / "lite.json.gz"
    bad.write_bytes(gzip.compress(json.dumps([{"instance_id": "x"}]).encode()))
    monkeypatch.setattr(instances, "DATA", bad)
    with pytest.raises(RuntimeError, match="hash mismatch"):
        instances.load_all()


def test_the_official_grader_does_not_count_a_reverse_applying_patch_as_applied():
    """The ported grader's source had an "already-applied" fallback: if every
    forward apply failed but `git apply --check --reverse` succeeded, the patch
    was recorded as applied and graded. Removed."""
    from types import SimpleNamespace

    from codepilot.bench.official_grader import apply_in_container

    seen = []

    class Container:
        def exec_run(self, cmd, workdir=None):
            seen.append(cmd if isinstance(cmd, str) else " ".join(cmd))
            text = cmd if isinstance(cmd, str) else ""
            ok = "--reverse" in text
            return SimpleNamespace(exit_code=0 if ok else 1, output=b"error: patch failed")

    applied, cmd, _ = apply_in_container(Container(), "/tmp/patch.diff",
                                         ["git apply --verbose", "patch --batch --fuzz=5 -p1 -i"])
    assert applied is False and cmd is None
    assert not any("--reverse" in c for c in seen)


def test_the_official_grader_will_not_pull_an_image_unless_asked():
    import docker

    from codepilot.bench.official_grader import ensure_image

    class Images:
        def get(self, name):
            raise docker.errors.ImageNotFound("absent")

        def pull(self, name):
            raise AssertionError("must not pull")

    with pytest.raises(RuntimeError, match="not present"):
        ensure_image(type("C", (), {"images": Images()})(), "swebench/sweb.eval.x86_64.x:latest")


def test_the_spend_ledger_does_not_move_with_the_output_file():
    """D38: the ledger defaulted to <out>.ledger.jsonl, so pointing --out at a
    new file started a fresh, empty ledger, and the run-wide cap with it."""
    import codepilot.llm
    from codepilot.bench.run import ledger_path

    a = ledger_path("bench/results/one.jsonl")
    b = ledger_path("somewhere/else/two.jsonl")
    assert a == b and a.parent == codepilot.llm.LEDGER_DIR, "the user-level ledger (D41)"


# A home directory as it appears on any of the three platforms, including the
# JSON-escaped `C:\\Users\\` form that a recording stores. The account name is
# captured so a failure can name it.
_HOME_SHAPED = re.compile(
    r"(?:/home/|/Users/|[A-Za-z]:[\\/]{1,2}Users[\\/]{1,2})([^/\\\"\s,;:)\]}]{1,64})"
)

# Verbatim dataset input, not something the harness wrote. `task.text` is the
# SWE-bench problem statement -- the issue body as it was filed -- and some of
# those bodies paste a traceback from the reporter's own machine. Redacting it
# would edit the task the model was given, so the guard reads it as given and
# checks only what this repository produces.
_VERBATIM_FIELDS = frozenset({"text"})


def _harness_written_text(path):
    """Every string in `path` that this repository authored, as one blob.

    A result file is JSON or JSON Lines; anything that does not parse is
    returned whole, because an unparseable file is exactly where a stray path
    would hide.
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    docs = []
    try:
        docs.append(json.loads(raw))
    except ValueError:
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                docs.append(json.loads(line))
            except ValueError:
                return raw  # not JSON at all; search all of it

    out = []

    def walk(node, key=None):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, list):
            for v in node:
                walk(v, key)
        elif isinstance(node, str) and key not in _VERBATIM_FIELDS:
            out.append(node)

    for d in docs:
        walk(d)
    return "\n".join(out)


def test_committed_results_carry_no_personal_paths():
    """E: recordings and result rows embedded home-directory paths.

    The needle used to be `Path.home().name`, the account running the test. That
    guarded one machine and nothing else: on a Linux CI runner the name is
    `runner`, which occurs in recorded patches and prose as an ordinary word
    (`runner.invoke(...)`, `get_runner(settings)`, "Django's test runner"), so
    the sweep matched benchmark content and the job went red with no leak
    present -- red since 2026-10-05, which also hid every real failure behind
    it. On the Windows runner the same test passed only because that account is
    called `runneradmin` and the string happens to be absent.

    So the check no longer depends on who is running it. It looks for a
    home-directory *shape* instead of one name, which catches any contributor's
    account rather than only the current machine's.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "bench" / "results"
    leaks = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        found = {m.group(0) for m in _HOME_SHAPED.finditer(_harness_written_text(p))}
        if found:
            leaks[p.relative_to(root).as_posix()] = sorted(found)
    assert not leaks, f"home-directory paths in committed results: {leaks}"


@pytest.mark.parametrize(
    "doc, expected",
    [
        ({"log_tail": "error at /home/someone/.cache/x"}, ["/home/someone"]),
        ({"log": "/Users/jane/Desktop"}, ["/Users/jane"]),
        ({"log": "C:\\\\Users\\\\bob\\\\AppData"}, ["C:\\\\Users\\\\bob"]),
        # Verbatim dataset input is read as filed, not edited.
        ({"task": {"text": "traceback /home/avinash/lib/y"}}, []),
        # The old needle was the running account's name, so on a Linux runner
        # these ordinary occurrences of "runner" failed the guard.
        ({"note": "runner.invoke(cli) and get_runner(x)"}, []),
    ],
)
def test_the_personal_path_guard_reads_only_what_the_harness_wrote(tmp_path, doc, expected):
    p = tmp_path / "x.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    found = sorted(m.group(0) for m in _HOME_SHAPED.finditer(_harness_written_text(p)))
    assert found == expected


def test_the_personal_path_guard_searches_a_file_it_cannot_parse(tmp_path):
    """An unparseable file is exactly where a stray path would hide."""
    p = tmp_path / "run.log"
    p.write_text("plain log /home/zoe/t", encoding="utf-8")
    assert "/home/zoe" in _harness_written_text(p)


def test_new_result_rows_have_the_home_directory_redacted():
    from pathlib import Path

    from codepilot.bench.run import redact

    home = str(Path.home())
    row = '{"log": "' + home.replace("\\", "\\\\") + '\\AppData\\x and ' + home.replace("\\", "/") + '/y"}'
    clean, _ = redact(row)
    assert Path.home().name not in clean and "<HOME>" in clean


def test_no_sampled_instance_appears_in_a_committed_arm_result():
    """C5/D45: the funded study's sample provably predates any measurement of
    the arms on it, so it cannot have been chosen to flatter one.

    `harness_check/` is excluded on purpose (D46): a gold/empty check calls no
    model and says nothing about either arm — it checks the environment and the
    grader — and the study's own first step runs exactly that check on all 20
    instances, so treating it as a result would forbid the plan's own procedure.
    """
    import pathlib

    from codepilot.bench.instances import sample

    ids = {r["instance_id"] for r in sample(20, 0)}
    results = pathlib.Path(__file__).resolve().parents[1] / "bench" / "results"
    # `harness_check` was always excluded: it makes no model call and judges the
    # environment, not either arm. `haiku-study` is excluded for a different and
    # weaker reason -- it IS an arm result, and a disclosed one. Until the study
    # ran, this guard could forbid every arm result outright; now that the study
    # is recorded, a blanket ban would forbid publishing the very thing the
    # sampling was for. So the ban narrows to *undisclosed* results, and the
    # study is pinned below so it cannot quietly grow to cover more instances.
    DISCLOSED = {"harness_check", "haiku-study"}
    committed = [p for p in results.rglob("*")
                 if p.is_file() and not (DISCLOSED & set(p.parts))]
    for p in committed:
        text = p.read_text(encoding="utf-8", errors="replace")
        overlap = sorted(i for i in ids if i in text)
        assert not overlap, f"{p.name} already holds an undisclosed result for {overlap}"

    # The pin: the study covers exactly the 10 instances its write-up reports, so
    # adding an eleventh arm result there fails here instead of passing silently.
    import json

    study = results / "haiku-study" / "main.jsonl"
    # The first row is the run's `config`, which carries no instance_id.
    studied = {row["instance_id"]
               for row in (json.loads(line) for line in study.read_text(encoding="utf-8").splitlines() if line.strip())
               if row.get("instance_id")}
    assert len(studied) == 10, f"the study covers {len(studied)} instances; its write-up reports 10"
    assert studied <= ids, "the study ran an instance outside sample(20, 0)"


def test_redact_catches_the_account_name_in_a_truncated_path():
    r"""A log tail cut mid-path kept the account name (found publishing the study).

    `redact` replaced only the full home directory, so `C:\Users\<name>\AppData\...`
    truncated to `<name>\AppData\...` -- which is what a captured `log_tail` holds
    once it is trimmed -- matched nothing and the name survived into committed
    results. `test_committed_results_carry_no_personal_paths` went red on exactly
    this, two commits after the study landed.
    """
    from pathlib import Path

    from codepilot.bench.run import redact

    name = Path.home().name
    for text in (
        name + r"\AppData\Local\Temp\bench-x\repo\django",      # plain Windows path
        name + r"\\AppData\\Local\\Temp\\bench-x",              # JSON-escaped, as stored
        name + "/AppData/Local/Temp/bench-x",                   # forward-slash form
    ):
        clean, n = redact(text)
        assert name not in clean, f"account name survived redaction of {text!r}"
        assert n >= 1
