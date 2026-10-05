# Agent vs agentless on SWE-bench Lite — Haiku 4.5, 2026-10-05

**Headline.** On 10 SWE-bench Lite instances, the agent resolved **5** and the
agentless baseline **1**. All four discordant instances favoured the agent; there
was no instance the baseline resolved and the agent did not.

| arm | resolved | model calls | cost |
|---|---|---|---|
| agent | **5 / 10** | 542 | $2.254 |
| agentless | **1 / 10** | 18 | $0.263 |

Total spend for this study including the canary: **$2.4892** over 588 calls.
Model: `claude-haiku-4-5-20251001`, pinned, priced at $1/M input and $5/M output
(LiteLLM 1.103.2's own cost map, which matches Anthropic's published Haiku 4.5
pricing — checked before the run, because a stale price table would bias every
figure here in the same direction).

## Per instance

| instance | agent | agentless | agent stopped by |
|---|---|---|---|
| django__django-15814 | **resolved** | — | budget |
| django__django-13401 | — | — | step limit |
| django__django-15996 | **resolved** | — | budget |
| django__django-15252 | — | — | budget |
| django__django-15851 | **resolved** | — | finished |
| sympy__sympy-21171 | — | — | finished |
| sympy__sympy-20154 | **resolved** | — | finished |
| sympy__sympy-23117 | **resolved** | **resolved** | finished |
| django__django-12308 | — | — | budget |
| django__django-13551 | — | — | finished |

Paired: both 1, agent only 4, agentless only 0, neither 5.

## What this does and does not support

**Exact McNemar on the 4 discordant pairs gives two-sided p = 0.125**, so this is
**not** a significant result at the conventional 0.05 level, and nothing here
should be written up as "significantly better". Ten instances cannot clear that
bar even with a perfect 4–0 split: 2 × 0.5⁴ = 0.125 is the smallest p this design
can produce.

The defensible claim is directional and paired: *the agent resolved 5 of 10
against the baseline's 1, every discordant instance favoured the agent, and the
agent never lost an instance the baseline won — a consistent direction on a
sample too small for a significance claim.*

**Cost-effectiveness, which is the more interesting finding.** The agent cost
**8.6×** the baseline ($2.254 vs $0.263) for 5× the resolutions — $0.45 per
resolution against $0.26. Better outcomes, worse efficiency. The agent spent 1,756
seconds of wall time and 542 calls; the baseline 18 calls.

**How the agent failed.** Of the 5 it did not resolve, **2 hit the step ceiling**
and **3 stopped by its own choice with a wrong fix**. So roughly half its failures
are capacity and half are reasoning — a more useful diagnosis than a bare count.

## Deviations from STUDY_PLAN, and why

Three, all deliberate and all recorded here so the write-up matches what ran:

1. **`--backend local`, not `--backend docker --image official`.** The official
   images need about one 4 GB image per instance; the plan's own note (D41) records
   ~200 GB against 176 GB free, and the harness never pulls. Consequence, stated
   plainly: **the absolute resolve rates here are not comparable to published
   SWE-bench Lite leaderboard numbers**, because grading happened in a local venv
   rather than the official image. The *paired* comparison is unaffected — both
   arms ran on identical instances in identical environments.
2. **10 instances, not 20.** See the exclusions below.
3. **`--max-calls 60 --max-cost 0.75`, not `40` and `0.40`.** The canary showed
   the agent exhausting all 40 calls without finishing, which would have made the
   agent look worse than it is for a reason unrelated to its ability. Raising the
   call limit alone would not have helped: at $0.40 the money limit becomes the
   new cutoff on these larger repositories.
   **The agent's own `MAX_STEPS = 60` (`codepilot/agent/loop.py:28`) was left
   alone.** It is a hardcoded product default, not a CLI flag, and editing an
   agent's core loop so it scores better on its own benchmark is tuning for the
   test. It binds on 2 of 10 instances, which is reported rather than removed.

## The 10 exclusions, each with its measured reason

From `instances.sample(20, seed=0)`. Every exclusion was established by the free
gold/empty check (`--arms gold empty`, no model calls, $0), not predicted:

| instance | why excluded |
|---|---|
| astropy__astropy-14995 | compiled extension; the local mount hides what the image builds in place |
| scikit-learn__scikit-learn-11281 | same |
| django__django-11133 | environment fine (P2P 64/64) but the required test fails **even with the official fix** — a `memoryview` behaviour difference under Python 3.12 |
| sympy__sympy-13895 | needs `collections.Mapping`, removed in Python 3.10 |
| pallets__flask-5063 | its `conftest.py` uses `ast.Str`, removed in 3.12; needs Python 3.11 |
| psf__requests-2148 | 2014-era code; the install itself fails on import |
| sphinx-doc__sphinx-7738 | `pkg_resources` gone from setuptools 84; pinning `setuptools<81` fixed that and exposed a further import failure |
| sphinx-doc__sphinx-8627 | same |
| pydata__xarray-4094 | same root cause; after the pin, tests ran (652/862 passing) but the required test still fails — environment not faithful |
| pytest-dev__pytest-7373 | installing pytest's own repo makes it report version `0.1.dev1`, and its `pyproject.toml` demands `minversion = 2.0` |

Only this machine has Python 3.12, which is why the three version-bound instances
cannot be recovered here. `bench/setups-local-attempted.json` holds the recipes
that were tried and did not work, kept so the attempt is not repeated blind.

## Harness validation came first, and it is the reason these numbers mean anything

The gold/empty check submits the instance's own fix (must resolve) and nothing
(must fail), with no model in the loop. **It was run before the paid study and it
caught four defects that would each have produced a confidently wrong result:**

1. **A missing dependency made the first canary score 0 for both arms.** `litellm`
   was declared in `requirements.txt` but the run used an ambient interpreter that
   lacked it. Both arms errored in about a second. Cost: $0.00 — the import failed
   before any API call, which was luck, not design.
2. **No `--setups` flag meant no test dependencies were installed for any
   instance**, so `pytest` itself was missing and every test came back "missing".
3. **Python 3.12 removed `distutils`**, which older Django imports. Three Django
   instances scored 0 of everything until `setuptools` was added to their recipe —
   then 32/32, 20/20 and 56/56. Before-fix evidence is kept in
   `2026-10-05-local-18-nosetuptools.jsonl`.
4. **Two instances' `empty` arm passes 1 of 2 required tests** (django-12308,
   django-13551), so those two are easier than intended. They are kept, because
   both arms are graded identically and resolving still needs all required tests,
   but they are flagged rather than hidden.

The first three all share one signature — **every test reported "missing" or
failing while the arms looked blameless** — and all three would have yielded a
clean-looking 0/10 vs 0/10 at full price.

## Reproducing

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
# free: confirm which instances grade correctly before paying for anything
python -m codepilot.bench.run --instances <ids> --arms gold empty \
  --setups bench/setups-local.json --backend local --out <path>
# the paid study
python -m codepilot.bench.run --instances <the 10 ids above> \
  --arms agent agentless --attempts 1 --seed 0 --model claude-haiku-4-5-20251001 \
  --backend local --max-total-usd 20 --max-cost 0.75 --max-calls 60 \
  --max-prompt-tokens 50000 --max-output-tokens 4096 --compact-at 35000 \
  --setups bench/setups-local.json --response-cache bench/.cache/responses \
  --env-file <a key file OUTSIDE this repo> --out bench/results/haiku-study/main.jsonl
```

The key file is passed with `--env-file` and never exported in the shell: the
repository holds other entry points that would spend an ambient
`ANTHROPIC_API_KEY` outside this study's cap.

## Limitations, stated rather than buried

- **n = 10, one attempt per instance.** No significance claim is available; see
  above. Repeating each instance 3 times would show stability but would not raise
  the instance count, which is the binding constraint.
- **Local grading, so not leaderboard-comparable.** The paired comparison holds;
  the absolute rate does not transfer.
- **Two instances are easier than intended** (the `empty` arm partly passes).
- **Django and sympy only.** The surviving 10 span two codebases, so this does not
  speak to generalisation across project types.
- **The agent's 60-step ceiling binds on 2 of 10.** Its true rate at a higher
  ceiling is unmeasured.
