# CodePilot-SWE

A coding agent that edits real git repositories, and a benchmark harness for
asking whether that kind of agent — a model choosing its own next step with
tools — beats a fixed, tool-free pipeline on real GitHub issues.

* **The agent** (`codepilot/`) reads code, edits it, runs the tests, and
  checkpoints every turn to a git ref so `codepilot undo` puts it back. It runs
  on any model LiteLLM can reach: Anthropic, OpenAI, Gemini, Groq (free tiers
  included), or a local server.
* **The benchmark** (`codepilot/bench/`) runs that agent and an Agentless
  baseline on SWE-bench Lite under one harness, with one selection rule, one
  grader, and the same prompt base and caching for both.

**What has been measured, and what has not.** The agent's design choices were
measured on CodePilot's own 20-task suite (below). The SWE-bench comparison
this repository is built for **has not been run**: the harness is tested
offline end to end, the plan and its cost are in
[`bench/STUDY_PLAN.md`](bench/STUDY_PLAN.md), and nothing here should be read
as a SWE-bench score.

---

## Where this came from

Two projects by the same author, merged with their history intact
(`git log` shows both; `git log --follow` traces moved files):

* **CodePilot-Agent** — the base: the agent loop, a fixed-pipeline comparison
  arm, tools, workspace, permissions, CLI, web UI, hosted mode, and a 20-task
  eval suite with committed results.
* **Autonomous-SWE-Agent** — the SWE-bench Lite harness, the Agentless
  baseline, a multi-provider LiteLLM client, a no-Docker workspace backend, a
  BM25 search tool, and a GitHub issue→PR integration.

What was kept, rewritten, ported and dropped — and the evidence for each — is
in [`docs/MERGE_DECISIONS.md`](docs/MERGE_DECISIONS.md). The design rationale
is in [`docs/DESIGN.md`](docs/DESIGN.md). In short: one agent (CodePilot's
loop) on a LiteLLM client; Autonomous-SWE-Agent's loop, tools, API and
frontend removed after their unique behaviour was ported; its harness and
Agentless pipeline rebuilt on CodePilot's substrate after several grading
defects were reproduced and fixed.

---

## Install

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # one key, for the provider of the model you use
codepilot doctor            # wiring checks; no key needed
```

## Use the agent

```bash
codepilot run "add retry logic to fetch.py"                    # default model: claude-opus-5
codepilot run --model gemini/gemini-2.5-flash "..."            # any LiteLLM model string
codepilot chat                                                 # a conversation; Ctrl-C steers
codepilot undo                                                 # revert the last turn
python -m codepilot.webui                                      # a local browser UI
python -m codepilot.integrations.github.solve <issue-url>      # issue in, diff out (--open-pr to push)
codepilot serve --check                                        # hosted mode; see DEPLOYING.md
```

A `CODEPILOT.md` at your repository root is added to the system prompt.

---

## Architecture

```
codepilot/
├── llm.py            the provider seam: LiteLLM, cache breakpoints, usage, cost, errors
├── providers.py      provider registry (which key a model needs, which have free tiers)
├── context.py        the conversation: history, compaction, cache breakpoint placement
├── workspace.py      real files: containment, read-before-write, line endings, checkpoint/undo
├── permissions.py    command allowlist and denylist, budgets, test-file protection
├── tools.py          11 tools, and the one place a tool call is executed
├── search_index.py   BM25 over 30-line chunks (the search_code tool)
├── indexer.py        AST symbol index (the find_symbol tool)
├── sandbox/          where commands run: local (tree-killing timeouts) and docker
├── agent/
│   ├── loop.py         the agent: the model chooses each step
│   └── pipeline.py     a fixed plan/code/test/debug/review graph (comparison arm)
├── bench/
│   ├── checkout.py     base-commit-only clone, baseline, diff, pristine restore
│   ├── environment.py  one task environment: local venv backend or Docker backend
│   ├── grading.py      what of an agent's diff may reach grading
│   ├── swebench.py     dataset, graded test command, exact-id grading
│   ├── testlog.py      per-test outcomes from pytest and Django logs
│   ├── prompts.py      the shared prompt base and each arm's half
│   ├── agentless/      localise, sample N patches
│   ├── selection.py    choosing among N candidates (both arms)
│   ├── harness.py      every arm on one instance, in one environment
│   ├── run.py          the CLI; estimate.py prices a study
│   └── suite/          CodePilot's own 20-task suite and its runner
├── integrations/     GitHub issue -> PR; Prometheus/OpenTelemetry from the event stream
├── cli.py, webui.py, server.py, http_api.py, session.py, replay.py, doctor.py
```

**One substrate.** Every arm — the loop, the fixed pipeline, Agentless — uses
the same client, the same event stream and the same safety layer. On SWE-bench
both arms also share the environment (cloned and set up once per instance,
restored before every attempt), the system-prompt base with its single cache
breakpoint, the temperature schedule, the selection rule and the grader. What
differs is tabulated in [`codepilot/bench/prompts.py`](codepilot/bench/prompts.py).

**Grading that cannot be gamed by the obvious routes.** Each was a reproduced
defect in one of the source projects (decisions D9, D16):

* the checkout holds the base commit only — no remote, no later history, so
  the gold fix is unreachable (`tests/test_bench_checkout.py`);
* grading restores a pristine tree and applies only the agent's source diff;
  test files, `conftest.py` and pytest configuration are dropped
  (`test_an_agent_written_conftest_cannot_flip_the_result` shows the conftest
  attack working when live, then failing when graded);
* every FAIL_TO_PASS and PASS_TO_PASS id is looked up exactly in the parsed
  log — no `-k` substring matching, no 20-test cap, no `-x`.

**Selection.** With N candidates (N agent attempts, or N Agentless samples),
every candidate is applied to a pristine tree, the tests nearest the changed
files are run in full, regressions are found test by test, and the largest
group of equivalent regression-free patches wins. Autonomous-SWE-Agent took
the first candidate that broke nothing, judged by pass/fail counts under `-x`.
Agentless's reproduction-test generation is **not** implemented.

---

## The benchmark

```bash
# Check the harness on an instance: no model, no key. gold must resolve, empty must not.
python -m codepilot.bench.run --instances pallets__flask-4992 --arms gold empty \
    --setups bench/setups.json

# Both arms, one attempt each
python -m codepilot.bench.run --instances pallets__flask-4992 --arms agent agentless \
    --model gemini/gemini-2.5-flash --setups bench/setups.json

# Budget-matched: 3 agent attempts vs 3 agentless samples, 50 sampled instances, Docker
python -m codepilot.bench.run --sample 50 --seed 0 --attempts 3 --arms agent agentless \
    --backend docker --image official --model ...

# CodePilot's own task suite
python -m codepilot.bench.suite.runner --dry-run
```

Results are written one JSON line per (instance, arm) as they finish, with
anything key-shaped redacted. A run the provider refused to serve is marked
`infra_error` and excluded, never scored as a failure.

**Backends.** `local` runs in a per-task virtualenv with bash and with
provider keys removed from the environment — **no isolation**: the model's
commands run as you, so use it only on repositories you would run yourself.
`docker` mounts the checkout into a container that loses its network before
the model's first command; `--image official` uses the instance's published
SWE-bench image. The Docker path is tested here against the one official
image present on the development machine (`pallets__flask-4992`), with the
local fixture task, and the harness check below ran `pallets__flask-4992`
through it end to end (gold resolved, empty did not). No model has been run
through it.

---

## What has been measured

### CodePilot's own suite (carried over, unchanged)

Measured by CodePilot-Agent on 1–2 September 2026, with Anthropic models,
before the merge. Raw files: [`bench/results/codepilot-suite/`](bench/results/codepilot-suite/).
Read them with these limits: **20 author-written tasks** (15 small, 5 on one
2,007-line fixture), **one run per configuration** (two for the loop arm on
the small tasks and for the large retrieval comparison), and a **ceiling
effect** — almost every configuration passed almost every task, so the pass
rates cannot separate the configurations; only cost can.

| experiment | finding | evidence |
|---|---|---|
| 1. loop vs fixed pipeline, 15 tasks, `claude-opus-5` effort low | both 15/15; cost per completed task $0.1838 (pipeline) vs $0.0422 (loop), 4.4x; the loop's repeat run cost $0.6396 against $0.6331, 1.0% apart | `2026-09-01-2210-arms.json`, `2026-09-01-2216-loop-repeat.json` |
| 2. `edit_file` vs `write_file` | 15 small tasks: `write_file` $0.0444 vs `edit_file` $0.0868 per completed task (15/15 both); 5 large tasks: `edit_file` $0.3691 vs `write_file` $0.6134, 1.66x (5/5 both); 0 of these 40 runs deleted a function that had to survive | `2026-09-01-2231-edit-style.json`, `2026-09-02-0456-large-edit-style.json` |
| 3. AST index vs regex search | no difference beyond noise: on the large fixture `find_symbol` was 6.8% cheaper than `search` in one run and 10.8% dearer in the repeat (5/5 everywhere; small tasks 14/14 vs 15/15, one run excluded as a provider outage) | `2026-09-01-2306-retrieval.json`, `2026-09-02-0502-…`, `2026-09-02-0509-…` |
| 4. routing vs effort, 5 large tasks | `claude-sonnet-5` $0.0869 vs `claude-opus-5` $0.5257 per completed task at effort low (6.0x), 5/5 each; Opus effort low→high moved cost 9.7% | `2026-09-02-1436-large-effort.json` |

The figures are recomputed from the files' `summary` blocks (cost per
completed task); CodePilot-Agent's original README quoted some as per-task
medians, which differ slightly (for example 4.19x for experiment 1). Across
all 150 recorded runs, none lost a function the task required to survive.

Two caveats added in the merge. The old runner graded in the agent's own tree,
so an agent-written `conftest.py` could have changed a result; none of the 150
recorded runs lists one among its edited files, but files created through
`run_command` are not recorded there (D16). And the tool set has since gained
`search_code`, so a configuration using "all tools" today is not the one
measured.

### Autonomous-SWE-Agent's recordings (carried over, unchanged)

Eight runs — its agentic loop and its agentless pipeline on four hand-picked
SWE-bench Lite instances, `claude-sonnet-5`, local backend, August 2026 — in
[`bench/results/autonomous-swe-agent-recordings/`](bench/results/autonomous-swe-agent-recordings/).
They were graded by the harness this merge replaced (20-test cap, `-k`
substring selection, `-x`, history-leaking clone), so their `resolved` flags
are not comparable to anything this harness produces. They are kept because
they are the only measured SWE-bench token counts available, and the study's
cost estimate is built on them.

### Harness validation (not a result)

The gold/empty check on the four smoke instances, no model calls
([`bench/results/harness_check/`](bench/results/harness_check/), 2026-10-02):
on `pallets__flask-4992` (Python 3.11 venv, and the official SWE-bench image)
and `sympy__sympy-18199`, `-22714`, `-24213` (Python 3.12 venv), the gold patch
resolves with every required test passing, and an empty patch leaves the
FAIL_TO_PASS test failing with every PASS_TO_PASS test passing. Running it
exposed a gap in the check itself, fixed before these rows were produced
(MERGE_DECISIONS D20).

### SWE-bench comparison

Not run. [`bench/STUDY_PLAN.md`](bench/STUDY_PLAN.md): 50 instances, 3 seeds,
both arms budget-matched at N = 3, an issue/gold-patch mismatch analysis, and a
priced estimate of **$4.67–$162.80** depending on the model (25.5M input and
1.4M output tokens, from the recordings' measured counts; roughly three times
that if the random instances are harder than the four recorded ones). A
free-tier smoke command is prepared there and has not been run.

---

## Limitations

* **No SWE-bench result yet**, as above. The harness is exercised end to end
  only on a local fixture task with a scripted model
  (`tests/test_bench_e2e.py`).
* **The in-repo grader is not the official one.** It applies the official
  criterion (every FAIL_TO_PASS and PASS_TO_PASS id passes), in this
  repository's environment. Django support is implemented from SWE-bench's own
  log format and unit-tested on log text only.
* **Selection without reproduction tests.** Agentless's strongest selection
  signal is missing, which likely understates that arm when N > 1.
* **Budget-matched means matched attempts, not matched dollars.** An agent
  attempt costs several times an Agentless sample; the cost columns show it.
* **The local backend is not a sandbox.** It narrows the blast radius
  (denylist, scrubbed keys, a temp checkout, tree-killing timeouts); it does
  not contain a model that is trying to escape.
* **Free tiers are rate-limited**, and whether a key is on a free tier is a
  property of the account that this code cannot check. Reported costs are list
  prices, not what a free tier bills.
* **Hosted mode is single-tenant**, and still reads the caller's key from an
  `X-Anthropic-Key` header, whatever the provider ([DEPLOYING.md](DEPLOYING.md)).
* **Python repositories only** in both benchmarks.

---

## Related work

* **SWE-agent** (Yang et al., 2024) argued that the *agent–computer interface*
  — purpose-built commands for viewing, searching and editing, with concise
  feedback — matters as much as the model. CodePilot's tools follow that line:
  ranged reads, exact-string edits that refuse ambiguous matches, errors
  returned as results.
* **Agentless** (Xia et al., 2024) showed that a fixed pipeline — hierarchical
  localisation, sampled repairs, regression filtering, reproduction tests and
  majority voting — competes with agents at a fraction of the cost. It is the
  baseline here, minus reproduction tests.
* **Aider's edit formats** (Gauthier, ongoing benchmarks) found that how a
  model is asked to express an edit — whole file, search/replace blocks, unified
  diff — changes both success and cost. CodePilot's experiment 2 is a small
  instance of the same question; Agentless here uses search/replace.
* **OpenHands** (Wang et al., 2024; formerly OpenDevin) is an open platform for
  software agents acting through a sandboxed shell, editor and browser, with
  CodeAct-style action spaces. It is the closest full-scale relative of the
  agent arm.
* **Reflexion** (Shinn et al., 2023) has agents reflect on failed attempts in
  language and retry. The budget-matched mode here deliberately does **not** do
  this: N attempts are independent, so the comparison with N independent
  samples stays like for like. Feeding one attempt's failure into the next is
  an obvious follow-up.

---

## Development

```bash
pytest -q          # 312 passed, 1 skipped (the opt-in Docker test); no key, no network
ruff check .
python -m codepilot.doctor
```

The tests use scripted models. The skipped test runs the Docker backend
against an image you name:
`CODEPILOT_DOCKER_IMAGE=swebench/sweb.eval.x86_64.pallets_1776_flask-4992:latest pytest -k docker`.
CI (`.github/workflows/ci.yml`) runs lint, tests and `doctor` on Ubuntu and
Windows with Python 3.11 and 3.12, checks the suite's fixtures, and builds both
Docker images.

Licence: MIT ([LICENSE](LICENSE)).
