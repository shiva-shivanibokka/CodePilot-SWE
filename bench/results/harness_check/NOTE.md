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
