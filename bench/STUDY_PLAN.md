# Study plan: a tool-using agent vs. Agentless on SWE-bench Lite

Status: **planned, not run.** No result in this repository answers the
question below yet. The free-tier smoke test that would prove the pipeline on
real instances has been prepared but not run (see "Before the study").

## Question

On the same model, with the same prompt base, the same caching strategy and
the same number of attempts, does a model-driven tool loop (CodePilot's agent)
resolve more SWE-bench Lite issues than a fixed localise-then-sample pipeline
(Agentless), and at what cost per resolved issue?

## Design

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

1. **Harness check, free.** For all 50 instances:
   `python -m codepilot.bench.run --sample 50 --seed 0 --arms gold empty --backend docker --image official`.
   Every `gold` must resolve and every `empty` must not. An instance where
   either fails is excluded *before* any model runs, and the exclusion is
   reported with the reason. This also measures how many official images
   build/run on the study machine.
2. **Smoke test, free tier.** Prepared, not run (the machine was needed for
   other work):

   ```bash
   python -m codepilot.bench.run \
     --instances pallets__flask-4992 sympy__sympy-18199 sympy__sympy-22714 sympy__sympy-24213 \
     --arms gold empty agent agentless --attempts 1 \
     --model gemini/gemini-2.5-flash --setups bench/setups.json --backend local \
     --max-calls 30 --max-tokens 300000 \
     --env-file ../Autonomous-SWE-Agent/.env \
     --out bench/results/smoke/$(date +%Y-%m-%d)-gemini-flash.jsonl
   ```

   These four instances are **not a sample**: they are the ones
   Autonomous-SWE-Agent already showed build on this Windows machine with the
   local backend (`bench/setups.json`). Results go under `bench/results/smoke/`
   and are a smoke test of the pipeline, not a result. A free-tier key may be
   rate-limited mid-run; such rows carry `infra_error` and are excluded.
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

**Where the token counts come from.** The free-tier smoke run, which was to
supply them, has not been run. The only measured SWE-bench token counts
available are the eight runs carried over from Autonomous-SWE-Agent
(`bench/results/autonomous-swe-agent-recordings/`, claude-sonnet-5, its own
prompts and tools, four hand-picked instances, no prompt caching):

* agent: **42,835** input and **1,567** output tokens per attempt (mean of 4);
* agentless: **10,337** input and **1,185** output tokens per model call
  (mean of 4 runs of 1 localisation + 4 samples).

Scaled to the design (150 instance-runs per arm; agent 3 attempts each;
agentless 1 localisation + 3 samples each): **25.48M input + 1.42M output
tokens** (agent 19.28M / 0.71M, agentless 6.20M / 0.71M).

| model | $/M in | $/M out | as measured (x1) | harder instances (x3) |
|---|---:|---:|---:|---:|
| claude-opus-5 | 5.00 | 25.00 | $162.80 | $488.39 |
| claude-sonnet-5 | 2.00 | 10.00 | $65.12 | $195.36 |
| claude-haiku-4-5 | 1.00 | 5.00 | $32.56 | $97.68 |
| gpt-5.6-terra | 2.00 | 12.00 | $67.95 | $203.85 |
| gemini/gemini-2.5-flash | 0.30 | 2.50 | $11.18 | $33.55 |
| gemini/gemini-3-flash-preview | 0.50 | 3.00 | $16.99 | $50.96 |
| groq/openai/gpt-oss-120b | 0.15 | 0.60 | $4.67 | $14.01 |
| groq/llama-3.3-70b-versatile ¹ | 0.59 | 0.79 | $16.15 | $48.45 |

¹ Not in LiteLLM's map; price from `codepilot.llm.PRICING` (Groq's published
on-demand price as known when written) — re-verify before paying.

Read the x1 column as optimistic: the four recorded instances are easy ones
(6–10 agent turns), and a random 50 will include instances where the agent
uses its whole 40-call budget. The x3 column is a judgement, not a
measurement. Hard upper bound from the caps: 450 agent attempts x 800k tokens
= 360M tokens for the agent arm alone, which no realistic run approaches but
which bounds the worst case. Caching (where the provider supports it) lowers
input cost; it is not assumed. The N = 1 secondary run adds roughly a third of
the agent arm and a half of the agentless arm.

**Recommendation.** Run the study first on `gemini/gemini-2.5-flash`
(about $11–34) or `groq/openai/gpt-oss-120b` (about $5–14) — or on their free
tiers, if the rate limits allow 150 instance-runs per arm in reasonable time —
then repeat on one Claude model if the first result is worth confirming.

## Not in this plan

* Agentless's reproduction-test generation (the paper's strongest selection
  signal). Adding it is the natural next experiment; it would change only
  `selection.py`.
* The official SWE-bench harness as a second grader. Worth running on the final
  submitted patches of one seed to confirm the in-repo grader agrees.
