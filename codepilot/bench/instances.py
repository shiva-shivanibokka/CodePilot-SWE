"""
SWE-bench Lite, frozen, and seeded sampling over it.

Copied from the Autonomous-SWE-Agent SOP-eval worktree
(`eval_sop/instances.py` and `eval_sop/data/swebench_lite.json.gz`,
uncommitted there, read on 2026-10-04; its branch head was 9911b2d), so the
exact rows a study draws from are part of this repository rather than
whatever the Hugging Face API serves on the day (docs/MERGE_DECISIONS.md, D37).

The dataset is `SWE-bench/SWE-bench_Lite` (test split, 300 rows), fetched from
the Hugging Face datasets-server rows API on 2026-10-01 at HF revision
b0dde1093fe417d83b7184254edf8199c1f0dff5. The sha256 of the uncompressed JSON
is checked on every load. Each row also carries the official `eval_script`,
`image` name and `log_parser`, which the optional official grader uses.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import random
from pathlib import Path

DATA = Path(__file__).parent / "data" / "swebench_lite.json.gz"
DATASET_ID = "SWE-bench/SWE-bench_Lite"
DATASET_REVISION = "b0dde1093fe417d83b7184254edf8199c1f0dff5"
DATASET_SHA256 = "7d87279f73305b067a954d450b68722ee9e16880d102fc9ff71c19840d1e12e4"


def load_all() -> list[dict]:
    raw = gzip.decompress(DATA.read_bytes())
    digest = hashlib.sha256(raw).hexdigest()
    if digest != DATASET_SHA256:
        raise RuntimeError(f"dataset hash mismatch: {digest}")
    return json.loads(raw)


def by_id() -> dict[str, dict]:
    return {r["instance_id"]: r for r in load_all()}


def seeded_order(seed: int) -> list[str]:
    """All 300 instance ids in a seeded random order (sorted first, so the
    order does not depend on how the rows arrived)."""
    ids = sorted(r["instance_id"] for r in load_all())
    rng = random.Random(seed)
    rng.shuffle(ids)
    return ids


def sample(k: int, seed: int) -> list[dict]:
    """The first `k` instances of `seeded_order(seed)`, as rows."""
    rows = by_id()
    return [rows[i] for i in seeded_order(seed)[:k]]
