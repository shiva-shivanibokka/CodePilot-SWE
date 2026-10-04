# Harness validation — not a result

`python -m codepilot.bench.run --arms gold empty`, 2026-10-02, on the four
smoke instances. No model was called. For each instance the harness submits
the instance's own fix (`gold`, must resolve) and nothing (`empty`, must run
the FAIL_TO_PASS tests and see them fail). This checks the environment and
the grader, not any agent.

| instance | backend | gold | empty |
|---|---|---|---|
| pallets__flask-4992 | local, Python 3.11 venv | resolved (F2P 1/1, P2P 18/18) | unresolved (F2P 0/1, P2P 18/18) |
| pallets__flask-4992 | docker, official SWE-bench image | resolved (F2P 1/1, P2P 18/18) | unresolved (F2P 0/1, P2P 18/18) |
| sympy__sympy-18199 | local, Python 3.12 venv | resolved (F2P 1/1, P2P 113/113) | unresolved (F2P 0/1, P2P 113/113) |
| sympy__sympy-22714 | local, Python 3.12 venv | resolved (F2P 1/1, P2P 11/11) | unresolved (F2P 0/1, P2P 11/11) |
| sympy__sympy-24213 | local, Python 3.12 venv | resolved (F2P 1/1, P2P 31/31) | unresolved (F2P 0/1, P2P 31/31) |

Files: `local-py311.jsonl`, `docker-official.jsonl`, `local-py312.jsonl`.
Setup commands are in each row (`bench/setups.json`); every setup exited 0.

`pre-fix/` holds the first run, before D20: there the `empty` rows never ran
any test (`F2P 0/0`), so they do not validate anything, and flask-4992 on
Python 3.12 failed even with the gold patch (D21). Kept as evidence.

The instances were chosen because they were known to build on this machine,
not sampled.

## 2026-10-04 — the free half of the sampling question (D46)

No model was called; nothing was paid for.

| file | what it is |
|---|---|
| `2026-10-04-mount-probe.json` | does the mounted checkout hide what the official image built in place, and can the image's own install repair it afterwards? Measured in the one official image on this machine. Yes, and no. |
| `2026-10-04-instance-classes.json` | the first 50 of `seeded_order(0)` classified by what could break grading, with the depth at which 20 survivors are reached under each assumption |
| `2026-10-04-django-local-py312.jsonl` | the first Django instance ever put through the gold/empty check: gold resolved (F2P 1/1, P2P 29/29), empty did not (F2P 0/1, P2P 29/29) |

The Django check **failed first**, and found two grader bugs (D46): the run
before the fix scored gold P2P **17/29** although Django itself printed
"Ran 30 tests ... OK", because 12 of the required ids are the tests'
*docstrings* — the id SWE-bench's own parser records — and because the label
list stopped at the first module a required test named. Both are fixed and
unit-tested; this file is the run after the fix. The larger draw (40-50
instances) was **not** run: it needs one official image each, about 200 GB
against 176 GB free, and the harness never pulls (D41).
