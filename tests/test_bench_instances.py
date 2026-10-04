"""The frozen dataset and the optional official grader (D37)."""

from __future__ import annotations

import json
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
