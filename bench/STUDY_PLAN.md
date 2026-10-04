# Study plan: a tool-using agent vs. Agentless on SWE-bench Lite

Status: **planned, not run.** No result in this repository answers the
question below yet. The harness check and a local-model smoke test of the
pipeline have been run (see "Before the study"). What is funded is a small
first run on one Claude model ("Funded study", with its exact commands); the
"Design" below is the full study, which is not funded.

## Question

On the same model, with the same prompt base, the same caching strategy and
the same number of attempts, does a model-driven tool loop (CodePilot's agent)
resolve more SWE-bench Lite issues than a fixed localise-then-sample pipeline
(Agentless), and at what cost per resolved issue?

## Funded study: $20 on claude-haiku-4-5-20251001

This repository's share of the available budget is **$25**. The study is sized
to keep a margin of more than 20% under it: the hard project maximum
(`bench.run.PROJECT_MAX_USD`) and every cap below is **$20**, and that cap
covers everything in the user-level ledger, the canary included (D41).

| | |
|---|---|
| model | `claude-haiku-4-5-20251001` ($1 / $5 per MTok; cache read $0.10, write $1.25 — D26) |
| instances | **20**, `instances.sample(20, seed=0)` from the frozen Lite rows (D37): django ×8, sympy ×4, sphinx ×2, and one each of astropy, flask, pytest, xarray, requests, scikit-learn |
| seeds | **1**. Anthropic takes no seed and Haiku does take temperature (D29), so a second seed would be a second sample, not a reproducible repeat |
| arms, N | agent and agentless, **N = 1** each (no selection), arm order randomised per instance (D35) |
| backend | Docker, official SWE-bench images (`--image official`): no network while the model acts, enforced (D10, D36) |
| caps | $0.40 per agent attempt; 40 model calls per attempt; 50,000 prompt tokens per request (larger ones are refused as an arm failure, D39); **4,096 output tokens per call in both arms** (`--max-output-tokens`, D45) — agentless alone re-asks once at double that when a reply is cut off mid-JSON, which an agent turn does not need because its next step continues; compaction at 35,000 prompt tokens, below the 50,000 bound, or the agent could never compact (D45); $20 run-wide, reserved atomically before every request (D28, D38, D41) |
| grading | this repository's clean-tree grader (D9) **only**. `resolved` is this harness's verdict, not a SWE-bench-comparable number: no second grader has been run, so there is no grader-agreement evidence. `official_grader.py` (D37) needs the `swebench` package, which is not installed, and `swebench_compat.py` is an import stub for it, not a grader. Running it on the submitted patches is listed under "Not in this plan" (D45) |

**Cost.** Expected, from the measured smoke tokens
(`codepilot.bench.estimate --results bench/results/smoke/2026-10-02-qwen2.5-7b.jsonl --instances 20 --seeds 1 --attempts 1`):
1.27M input + 0.05M output tokens = **$1.54** (x3 for harder instances:
**$4.61**), with no cache discount assumed. **Worst case from the caps**
(`--dry-run`, D39/D40): agent $11.32 + agentless $5.18 = **$16.50**, under the
$20 cap. On Haiku nothing under 4,096 tokens caches, so only the rolling
breakpoint (D34) can save anything.

**Not comparable to the full design below:** 20 instances, one sample per arm
and one model can show whether the pipeline produces meaningful numbers and
roughly where the arms stand; a 20-instance difference smaller than about 25
points is not distinguishable, and the report must say so.

### Why this sample cannot have been chosen to flatter the result

`instances.sample(20, seed=0)` is deterministic over a frozen dataset whose
sha256 is verified on load, at a pinned revision (D37), and **no committed
result measures either arm on any of the 20** — not the carried-over
recordings, not the smoke run. The sample therefore provably predates any
measurement of the thing being compared. (One of the 20,
`django__django-15814`, does appear in a committed **harness check**: a
model-free gold/empty validation of the environment and grader, which reveals
nothing about either arm. The study's own first step runs that check on all 20
for the same reason, D46.)

### Proposal, NOT ADOPTED: how the sample would survive the harness check

**Nothing in this section is in force.** The active sample is still
`instances.sample(20, seed=0)`, and the runbook's instance list is unchanged.
The user decides; this records what the alternatives cost, measured rather
than predicted (D46).

**The candidate rule.** *"The first 20 instances of `seeded_order(0)` that pass
the gold/empty harness check."* It keeps the provenance argument above intact:
`seeded_order(0)` is a fixed permutation of a sha256-verified dataset at a
pinned revision, and the check is model-free, so the survivor set is decided
before a cent is spent and cannot be influenced by any result.

**What was measured (free, no model, no spend).** The committed harness check
covers flask-4992 and three sympy instances. For the rest, the two things that
could break grading were examined directly instead of guessed at:

1. **The mount really does shadow the image's build.** Probed inside the one
   official image on the development machine: with the checkout mounted over
   `/testbed`, `find` reports **no `.so` and no `.egg-info`** — the image's
   in-place build is hidden. For flask this is harmless (`import flask`
   resolves to `/testbed/src/flask/__init__.py`, exit 0), because the package
   is pure Python and the image's path configuration points at that same path.
   It is **not** harmless for a repository whose import needs a compiled
   extension, because the extension is in the hidden layer.
2. **The repair cannot happen after setup.** The official eval script's own
   `python -m pip install -e .` fails in our container — the network is
   already disconnected by then (`Temporary failure in name resolution`). So
   if this is to be fixed, it must be fixed during setup, while the network is
   still up, where `BenchEnv.create` currently skips the install for official
   images on the grounds that they are already installed.

So the at-risk class is narrower than this plan first claimed: **compiled
extensions** (astropy, scikit-learn, matplotlib), not Django. Django is pure
Python, and the grader runs its own test runner and parses its output
(`TestSpec(kind="django")`), so its risk is the runner, not the mount. That
risk was checked rather than assumed, and it was real: the first Django
instance ever put through the gold/empty check **failed it**, and the two
grader bugs behind that are now fixed (D46). `django__django-15814` now passes
on the local backend — gold F2P 1/1, P2P 29/29; empty F2P 0/1. Whether a
Django instance also passes inside its official image is still open.

| class | first 20 | first 50 | why |
|---|---:|---:|---|
| pure Python + pytest | 10 | 21 | flask, sphinx, sympy, xarray, requests, pytest, seaborn — the class already validated |
| Django's own runner | 8 | 21 | pure Python, so not shadowed; two grader bugs found and fixed here, one instance now passing on the local backend, none yet checked in an official image (D46) |
| compiled extension, shadowed | 2 | 8 | astropy ×2, scikit-learn ×2, matplotlib ×4 |

**n under each option, from the counts above rather than from a prediction.**
The two "and Django fail" rows are what the numbers were *before* the grader
bugs of D46 were fixed; they are kept because they bound the pessimistic case,
not because they are still expected.

| option | n | cost to find out | note |
|---|---:|---|---|
| keep the committed 20, compiled repos fail | **18** | free | astropy-14995 and scikit-learn-11281 drop |
| keep the committed 20, compiled *and* Django fail | **10** | free | the pessimistic case this plan first assumed |
| candidate rule, compiled repos fail | **20** | free | the first **23** of `seeded_order(0)` |
| candidate rule, compiled *and* Django fail | **20** | free | the first **45** of `seeded_order(0)` |
| fix the setup install, keep the committed 20 | **20** | a code change + a re-check | run the image's own install while the network is up |

**The cost of the candidate rule is survivorship bias**, and it must be
reported if the rule is adopted: dropping the repositories whose environments
are hardest to build makes the **absolute** resolve rates optimistic. It does
**not** bias the comparison, because both arms get exactly the same instances.

**What would settle it, and what it costs.** Nothing above needed any credits,
but the real check on the two open classes needs the official images, and the
harness never pulls (D41). The whole first 50 is about 200 GB uncompressed
against 176 GB free on the development machine, so that draw cannot be checked
here at all. One image per open class — one astropy or scikit-learn, one
django — is about 8 GB and would answer both questions outright. That is
bandwidth and disk, not money, and it is an operator decision.

### Caveats, known before spending

* **No seed on Claude; temperature is sent on Haiku** (D29). Repeating the run
  does not reproduce it; the response cache (`--response-cache`) does.
* **Compiled-extension repos may fail the harness check:** the checkout is
  mounted over the image's `/testbed`, which hides whatever the image built in
  place — measured, not assumed: with the mount in place `find` reports no
  `.so` and no `.egg-info` under `/testbed` (D46). That is harmless for a pure
  Python package and fatal for one whose import needs a compiled extension, so
  the at-risk instances in the committed sample are **astropy-14995 and
  scikit-learn-11281**, not the 10 an earlier version of this plan predicted.
  Django is pure Python; its separate risk was that the grader drives Django's
  own test runner (`TestSpec(kind="django")`, `parse_django`). Two bugs there
  were found and fixed on 2026-10-04 (D46), and
  `bench/results/harness_check/2026-10-04-django-local-py312.jsonl` is the
  instance passing afterwards; no Django instance has yet been checked inside
  an official image. Instances that fail the check are excluded and reported,
  not fixed during the study.
* **Agentless regression gate on Django** is skipped (D12); measuring it with
  Django's own runner and log parser (the SOP-eval worktree's approach, D37)
  is future work, listed below.
* **The $20 cap bounds RECORDED worst case, not the provider's bill** (D45).
  Every request is reserved at its worst case before it is sent, so the
  *ledger* can never pass $20 — but LiteLLM's transport-level reconnect can
  bill a request the ledger records once (D41). In the expected case
  (about $1.54) a few duplicates are noise; in the stated worst case the bill
  would reach about **$33** if *every* call double-billed, which is over this
  repository's $25 share though inside the $50 total. Earlier wording
  ("inside the 20% margin") was true of the expected case only. Mitigation is
  procedural: read the provider's own usage page after the canary and again
  after the first few instances of the main run, and stop if billed spend
  diverges from the ledger.
* **Thinking and compaction are not exercised live** (D31): Haiku does not
  think unless asked, so the study does not depend on it.

### Runbook

Every command below is run exactly as written by
`tests/test_runbook.py`, against a fake model client, so the documented
commands are known to parse, to respect the hard maximum, to take the lock and
to write the ledger.

1. **Pull the images, checking sizes first.** The harness never pulls
   (D41). List them with
   `python -c "from codepilot.bench.instances import sample; print('\n'.join(sorted({r['image'] for r in sample(20, 0)})))"`
   and `docker pull` each one; ~4 GB each uncompressed (flask-4992's is
   4.23 GB), 178 GB were free on the planning machine.
2. **Harness check, free** (no model): every gold patch must resolve and every
   empty patch must leave its FAIL_TO_PASS tests failing (D20). Instances that
   fail are excluded before anything is spent, and listed.

<!-- runbook:harness-check -->
```bash
python -m codepilot.bench.run --sample 20 --seed 0 --arms gold empty --backend docker --image official --out bench/results/haiku-study/harness-check.jsonl
```

3. **Dry runs**, which must both exit 0:

<!-- runbook:dry-run -->
```bash
python -m codepilot.bench.run --instances pallets__flask-5063 --arms agent agentless --attempts 1 --seed 0 --model claude-haiku-4-5-20251001 --backend docker --image official --max-total-usd 1 --max-cost 0.40 --max-calls 40 --max-prompt-tokens 50000 --max-output-tokens 4096 --compact-at 35000 --dry-run
python -m codepilot.bench.run --sample 20 --seed 0 --arms agent agentless --attempts 1 --model claude-haiku-4-5-20251001 --backend docker --image official --max-total-usd 20 --max-cost 0.40 --max-calls 40 --max-prompt-tokens 50000 --max-output-tokens 4096 --compact-at 35000 --dry-run
```

4. **Canary**: one instance, $1 cap. `pallets__flask-5063` is the sample's
   flask instance; if it fails the harness check, use the first instance of
   the sample that passed. Read every row and the ledger before going on.

<!-- runbook:canary -->
```bash
python -m codepilot.bench.run --instances pallets__flask-5063 --arms agent agentless --attempts 1 --seed 0 --model claude-haiku-4-5-20251001 --backend docker --image official --max-total-usd 1 --max-cost 0.40 --max-calls 40 --max-prompt-tokens 50000 --max-output-tokens 4096 --compact-at 35000 --response-cache bench/.cache/responses --env-file .env --out bench/results/haiku-study/canary.jsonl
```

5. **Reconcile the bill against the ledger** — the only mitigation for the
   double-billing gap above, so it is a step, not advice. Read the ledger's
   total and the provider's own usage page for the same window, and compare:

```bash
python -c "from codepilot.llm import Ledger; l=Ledger.default(); print(l.path); print('ledger total $%.4f' % l.total_usd())"
```

   Do this after the canary and again after the first two or three instances of
   the main run. If the provider's figure runs ahead of the ledger's by more
   than a rounding difference, stop the run (Ctrl-C releases the lock and
   leaves every unsettled request charged at its worst case) and report the
   divergence before spending anything more.

6. **Main run**: the 20 instances (minus harness-check exclusions — pass
   `--instances` with the survivors instead of `--sample` if any failed). The
   $20 cap includes the canary's spend; the canary instance is served from the
   response cache.

<!-- runbook:main -->
```bash
python -m codepilot.bench.run --sample 20 --seed 0 --arms agent agentless --attempts 1 --model claude-haiku-4-5-20251001 --backend docker --image official --max-total-usd 20 --max-cost 0.40 --max-calls 40 --max-prompt-tokens 50000 --max-output-tokens 4096 --compact-at 35000 --response-cache bench/.cache/responses --env-file .env --out bench/results/haiku-study/main.jsonl
```

7. If a run dies and its lock stays behind: `python -m codepilot.bench.run
   --break-stale-lock` (removes it only if its process is gone). Unsettled
   requests stay charged at their worst case in the ledger
   (`~/.sop_eval/codepilot_swe/ledger.sqlite`, which does not follow `%LOCALAPPDATA%`, D45).

## Design (full study, not funded)

| | |
|---|---|
| instances | **50** from SWE-bench Lite (300), sampled with `random.Random(0).sample(sorted by instance_id, 50)` — `python -m codepilot.bench.run --sample 50 --seed 0` |
| seeds | **3** (0, 1, 2). The seed fixes the model's sampling where the provider honours `seed` (agent attempt k sends `seed*1000+k`, agentless sample k `seed*1000+500+k`). The instance sample stays seed 0 for all three, so the seeds are repeats on the same 50 instances. |
| arms | `agent` (CodePilot's loop) and `agentless` |
| budget matching | **N = 3**: 3 independent agent attempts vs 3 agentless samples per instance, both chosen among by the same rule (`codepilot/bench/selection.py`). Matched in attempts, not dollars; cost is reported per arm. A secondary N = 1 run (no selection) separates "the loop" from "the loop plus selection". |
| model | one model for both arms per study; the first study on a model with a published price and a provider that honours `seed` |
| per-attempt caps | 40 model calls, 800k tokens, $1.00 at list price (`--max-calls`, `--max-tokens`, `--max-cost`) |
| backend | Docker with official SWE-bench images (`--backend docker --image official`), so every instance has the environment SWE-bench built for it; the checkout is mounted over `/testbed` and the container has no network while the model acts |
| grading | `codepilot/bench/swebench.py::grade`: pristine tree, agent's source diff only, then the test patch; every FAIL_TO_PASS and PASS_TO_PASS id looked up exactly |

What differs between arms, and nothing else: the arm-specific half of the
system prompt, the user message (issue vs. issue + map / issue + file), tool
schemas (agent only), and turn structure. Table in `codepilot/bench/prompts.py`.

## Before the study

1. **Harness check, free.** Done for the four smoke instances on
   2026-10-02 (`bench/results/harness_check/`): gold resolved and empty did
   not on all four. For the study, repeat it on all 50:
   `python -m codepilot.bench.run --sample 50 --seed 0 --arms gold empty --backend docker --image official`.
   Every `gold` must resolve and every `empty` must run its FAIL_TO_PASS
   tests and see them fail (D20). An instance where
   either fails is excluded *before* any model runs, and the exclusion is
   reported with the reason. This also measures how many official images
   build/run on the study machine.
2. **Smoke test.** Done on 2026-10-02 on a local model, `ollama/qwen2.5:7b`
   with a 16k context (no Gemini key was available):
   `bench/results/smoke/` and its `NOTE.md` hold the command, the caps and
   every row. Every stage ran on all four instances and both arms; 0/8
   resolved, as expected of a 7B model; one agent patch reached grading on a
   pristine tree. It exposed two harness problems, fixed before the committed
   run (docs/MERGE_DECISIONS.md, D23 transcripts in result rows, D24 the
   Ollama tool-calling route), and confirmed the overflow guard (D22): one
   agentless file did not fit 16k and was counted as a failure.

   On this machine `pallets__flask-4992` needs a Python 3.11 venv
   (`--python <3.11 interpreter>`) or the official image
   (`--backend docker --image official`); under 3.12 even its gold patch
   fails (D21). The four instances are **not a sample**: they are the ones
   known to build here.

   A hosted-model smoke run with the study's own model should still precede the
   study; the command is the one in `NOTE.md` with `--model` changed and the
   `num_ctx`/output caps removed.
3. **Re-price** from the smoke run's own token counts:
   `python -m codepilot.bench.estimate --results bench/results/smoke/<file>.jsonl`.

## Analysis

* **Primary:** resolve rate per arm, averaged over the 3 seeds, with a paired
  per-instance comparison (each instance contributes agent-resolved vs
  agentless-resolved under the same seed). Report the McNemar test on the
  pooled discordant pairs and a bootstrap CI over instances (10,000 resamples,
  instances resampled, seeds kept). With 50 instances, a difference smaller
  than roughly 10 points will not be distinguishable; say so rather than
  reading a smaller gap as a finding.
* **Variance:** per-seed resolve rates per arm, so run-to-run noise is
  reported before any gap is.
* **Cost:** USD per instance and per resolved instance, per arm, at list
  price; tokens in/out/cached; model calls; wall time.
* **Selection:** how often the selected candidate is the resolving one when at
  least one of the N resolves (oracle@N vs. selected@N), per arm. This is
  where agentless's lack of reproduction tests should show.
* **Exclusions:** infra errors, instances whose gold patch fails, and
  environment build failures, each counted and listed, never scored as 0.
* **Stratification:** by repository and by SWE-bench Verified difficulty label
  where one exists (`swebench.load_difficulty_labels`, ~1/3 of Lite).

### Issue / gold-patch mismatch analysis

Some Lite instances are unfair to *any* solver: the issue under-specifies what
the hidden tests check (a function name, an error message, a new parameter's
name), or the gold patch does more than the issue asks. Resolve rates on those
measure guessing. Before unblinding the arm results:

1. For each of the 50 instances, two readers who have not seen any arm's
   output label it from the issue text and the test patch alone:
   *specified* (the tests check only behaviour the issue states),
   *under-specified* (the tests require a name/message/signature the issue does
   not give), or *mismatched* (the tests check behaviour the issue does not ask
   for). Disagreements resolved by discussion; Cohen's kappa reported.
2. Cross-check against SWE-bench Verified, whose annotators filtered for the
   same problems: instances in both sets carry Verified's judgement.
3. Report resolve rates for all 50 and for the *specified* subset. A
   conclusion that holds only on the full set is about guessing, not about the
   arms.

## Cost estimate

Computed by `python -m codepilot.bench.estimate` (reproducible; prices from
LiteLLM 1.103.2's cost map unless marked).

**Where the token counts come from.** Two measured sources, and the plan is
priced on the newer one:

1. **This repository's smoke run** (`bench/results/smoke/`, qwen2.5:7b, the
   harness and prompts the study will use, 4 instances, N = 1):
   agent **52,019** input / **2,023** output tokens per attempt (mean of 4);
   agentless **5,845** input / **303** output tokens per call (mean of 7 calls
   over 4 runs).
2. The eight runs carried over from Autonomous-SWE-Agent (claude-sonnet-5, its
   own prompts and tools): agent 42,835 / 1,567 per attempt; agentless 10,337
   / 1,185 per call. Kept for comparison: it put the total at 25.48M input +
   1.42M output tokens and the same dollar range within 4%.

Scaled to the design (150 instance-runs per arm; agent 3 attempts each;
agentless 1 localisation + 3 samples each), from the smoke run:
**26.92M input + 1.09M output tokens** (agent 23.41M / 0.91M, agentless
3.51M / 0.18M). `python -m codepilot.bench.estimate --results
bench/results/smoke/2026-10-02-qwen2.5-7b.jsonl [--hardness 3]`:

| model | $/M in | $/M out | as measured (x1) | harder instances (x3) |
|---|---:|---:|---:|---:|
| claude-opus-5 | 5.00 | 25.00 | $161.88 | $485.65 |
| claude-sonnet-5 | 2.00 | 10.00 | $64.75 | $194.26 |
| claude-haiku-4-5 | 1.00 | 5.00 | $32.38 | $97.13 |
| gpt-5.6-terra | 2.00 | 12.00 | $66.94 | $200.81 |
| gemini/gemini-2.5-flash | 0.30 | 2.50 | $10.81 | $32.42 |
| gemini/gemini-3-flash-preview | 0.50 | 3.00 | $16.73 | $50.20 |
| groq/openai/gpt-oss-120b | 0.15 | 0.60 | $4.69 | $14.08 |
| groq/llama-3.3-70b-versatile ¹ | 0.59 | 0.79 | $16.74 | $50.23 |

¹ Not in LiteLLM's map; price from `codepilot.llm.PRICING` (Groq's published
on-demand price as known when written) — re-verify before paying.

Why the x1 column is a floor, not a forecast:

* The smoke model stopped early. Every agent run ended by answering in prose
  after 4–13 calls, well inside the 25-call cap, and agentless samples were
  short or malformed. A capable model will keep working and write longer
  replies; the study's cap is 40 calls per attempt.
* Counts are in Qwen's tokenizer; other providers' tokenizers count the same
  text somewhat differently.
* The four instances are hand-picked, easier than a random 50.

The x3 column is a judgement, not a measurement. Hard upper bound from the
caps: 450 agent attempts x 800k tokens = 360M tokens for the agent arm alone.
Caching (where the provider supports it) lowers input cost; it is not assumed.
The N = 1 secondary run adds roughly a third of the agent arm and a half of
the agentless arm. Re-price from the first hosted-model smoke run before
committing money.

**Recommendation.** Run the study first on `gemini/gemini-2.5-flash`
(about $11–34) or `groq/openai/gpt-oss-120b` (about $5–14) — or on their free
tiers, if the rate limits allow 150 instance-runs per arm in reasonable time —
then repeat on one Claude model if the first result is worth confirming.

## Not in this plan

* Agentless's regression gate on Django, measured with Django's own test
  runner and log parser instead of being skipped (D12, D37).
* Agentless's reproduction-test generation (the paper's strongest selection
  signal). Adding it is the natural next experiment; it would change only
  `selection.py`.
* Running the official SWE-bench harness as a second grader, and so any
  grader-agreement number. It is wired in
  (`codepilot/bench/official_grader.py`, D37) but has never been run: the
  `swebench` package is not installed and nothing in this repository calls it
  (`swebench_compat.py` is an import stub for that package, not a grader).
  Until it runs on the funded study's submitted patches, every `resolved`
  figure here is this harness's own verdict.
