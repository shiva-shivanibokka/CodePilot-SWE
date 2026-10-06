"""Turn the committed Haiku study into the JSON the replay site serves.

The site is static: it replays a run that already happened and makes no model
call, so it needs no key, no backend and no budget. `bench/results/haiku-study/`
is the only input, and this script is the only way the site's data is produced --
nothing here is written by hand, so the page cannot drift from the study.

Layout written under `public/data/`:

  index.json            config, arm totals, one summary row per instance
  run-<instance>-<arm>.json   one run: grade, diff, full transcript

Split per run rather than served as one bundle because `main.jsonl` is 760 KB and
a visitor opening one instance should not download the other nine.

Every string that reaches `public/data/` goes through the harness's own
`redact()` -- the same function the study's results went through -- and this
script *fails* rather than writes if it finds anything to redact. That is
deliberate: a redaction performed here would mean the committed study files are
themselves unclean, which is a problem to fix at the source, not to paper over at
publication time. The account name once reached two committed result files
because a scan required a `C:\\Users\\` prefix that log truncation had already
removed, so this check looks for the bare name too (`redact` does that now).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
STUDY = REPO / "bench" / "results" / "haiku-study"
OUT = HERE / "public" / "data"

sys.path.insert(0, str(REPO))
from codepilot.bench.run import redact  # noqa: E402

# Kept to what the page shows, so a reader can check the page against the file
# without scrolling past fields it ignores. `log_tail` is deliberately excluded:
# it is the largest field, it is the one truncation damaged, and the page shows
# the structured pass/fail counts instead.
GRADE_FIELDS = (
    "resolved", "applied", "f2p_passed", "f2p_total",
    "p2p_passed", "p2p_total", "detail", "command", "exit_code",
)
RUN_FIELDS = (
    "instance_id", "repo", "arm", "model", "resolved", "submitted",
    "cost_usd", "model_calls", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_write_tokens", "wall_seconds",
    "stopped_by", "changed_lines", "selection_basis", "notes",
    "contamination", "error", "infra_error", "timestamp",
)


def clean(value):
    """`value` with nothing left to redact, or raise saying where."""
    if isinstance(value, str):
        out, n = redact(value)
        if n:
            raise SystemExit(
                f"refusing to publish: {n} redaction(s) needed in a committed study "
                f"file. Fix the source in {STUDY}, do not redact at publish time.\n"
                f"  offending text starts: {value[:120]!r}"
            )
        return out
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def main() -> int:
    rows = [json.loads(line) for line in (STUDY / "main.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    config = next(r for r in rows if r.get("arm") == "config")
    runs = [r for r in rows if "instance_id" in r]

    OUT.mkdir(parents=True, exist_ok=True)
    for stale in OUT.glob("*.json"):
        stale.unlink()

    summary: dict[str, dict] = {}
    totals: dict[str, dict] = {}
    for r in runs:
        arm, iid = r["arm"], r["instance_id"]
        grade = r.get("grade") or {}
        run = {k: clean(r.get(k)) for k in RUN_FIELDS}
        run["grade"] = {k: clean(grade.get(k)) for k in GRADE_FIELDS}
        run["diff"] = clean(r.get("diff") or "")
        run["transcript"] = clean(r.get("transcript") or [])
        (OUT / f"run-{iid}-{arm}.json").write_text(
            json.dumps(run, indent=1, sort_keys=True), encoding="utf-8"
        )

        slot = summary.setdefault(iid, {"instance_id": iid, "repo": r.get("repo", ""), "arms": {}})
        slot["arms"][arm] = {
            "resolved": bool(r.get("resolved")),
            "stopped_by": clean(r.get("stopped_by") or []),
            "cost_usd": r.get("cost_usd", 0.0),
            "model_calls": r.get("model_calls", 0),
            "wall_seconds": r.get("wall_seconds", 0.0),
            "transcript_events": len(r.get("transcript") or []),
        }
        t = totals.setdefault(arm, {"resolved": 0, "runs": 0, "model_calls": 0, "cost_usd": 0.0})
        t["runs"] += 1
        t["resolved"] += bool(r.get("resolved"))
        t["model_calls"] += r.get("model_calls", 0) or 0
        t["cost_usd"] += r.get("cost_usd", 0.0) or 0.0

    # Ordered as the study ran them, not alphabetically, so the page matches
    # `RESULTS.md`'s per-instance table row for row.
    order = [i for i in config.get("instances", []) if i in summary]
    order += [i for i in summary if i not in order]

    index = {
        "model": config.get("model"),
        "seed": config.get("seed"),
        "attempts": config.get("attempts"),
        "max_calls": config.get("max_calls"),
        "backend": config.get("backend"),
        "arms": config.get("arms", []),
        "totals": totals,
        "instances": [summary[i] for i in order],
    }
    (OUT / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")

    written = sorted(p.name for p in OUT.glob("*.json"))
    size = sum((OUT / n).stat().st_size for n in written)
    print(f"wrote {len(written)} files, {size / 1024:.0f} KB, to {OUT}")
    for arm, t in sorted(totals.items()):
        print(f"  {arm:<10} {t['resolved']}/{t['runs']} resolved, "
              f"{t['model_calls']} calls, ${t['cost_usd']:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
