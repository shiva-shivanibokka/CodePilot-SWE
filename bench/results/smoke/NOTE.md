# Smoke test of the pipeline — not a result

2026-10-02. Both arms, end to end, on the four instances that passed the
harness check (`../harness_check/`), on a local model. The point was to show
that every stage runs on real SWE-bench instances: checkout, setup, the
agent's tools, agentless localisation and sampling, selection, clean-tree
grading, accounting. **A resolve rate of 0/8 was expected and is what
happened; it says nothing about either arm.** The instances were chosen
because they build on this machine, not sampled.

## Configuration

| | |
|---|---|
| model | `ollama/qwen2.5:7b` (Q4_K_M, 7.6B), local Ollama, sent to `/api/chat` (D24) |
| context | `num_ctx=16384`; requests that would not fit are refused, not truncated (D22) |
| arms, attempts, seed | agent and agentless, N = 1, seed 0 |
| per-attempt caps | 25 model calls, 250,000 tokens, 1,536 output tokens per agent call, compaction at 9,000 tokens; agentless sample cap 4,096 output tokens (its default) |
| backends | flask-4992: Docker, official SWE-bench image (already on the machine). sympy: local venv (Python 3.12), throwaway clones in the temp directory, provider keys scrubbed |
| cost | $0 — local model; `cost_usd` is 0 because no price exists for it |
| measured prompt size | agent's first request: 1,762–2,146 tokens (system prompt + 11 tool schemas + issue), measured by Ollama |

Command (per instance; flask with `--backend docker --image official`, sympy
with `--backend local --setups bench/setups.json`):

```
python -m codepilot.bench.run --instances <id> --arms agent agentless --attempts 1 --seed 0 \
  --model ollama/qwen2.5:7b --model-option num_ctx=16384 --max-output-tokens 1536 \
  --max-calls 25 --max-tokens 250000 --compact-at 9000 \
  --out bench/results/smoke/2026-10-02-qwen2.5-7b.jsonl
```

## Results (`2026-10-02-qwen2.5-7b.jsonl`)

Tokens are Ollama's counts (Qwen's tokenizer). Input is the total over every
call, so a long agent conversation counts its history once per call.

| instance | arm | backend | calls | input tokens | output tokens | wall | stopped by | patch submitted | graded |
|---|---|---|---:|---:|---:|---:|---|---|---|
| pallets__flask-4992 | agent | docker | 4 | 11,462 | 684 | 28s | ended without finish | no | not resolved |
| pallets__flask-4992 | agentless | docker | 2 | 10,512 | 416 | 19s | 0 of 1 samples usable | no | not resolved |
| sympy__sympy-18199 | agent | local | 13 | 82,785 | 2,380 | 290s | ended without finish | yes | not resolved (F2P 0/1, P2P 113/113) |
| sympy__sympy-18199 | agentless | local | 1 | 7,167 | 94 | 8s | 0 of 1 samples usable | no | not resolved |
| sympy__sympy-22714 | agent | local | 8 | 44,505 | 2,739 | 142s | ended without finish | no | not resolved |
| sympy__sympy-22714 | agentless | local | 2 | 13,432 | 1,392 | 41s | 0 of 1 samples usable | no | not resolved |
| sympy__sympy-24213 | agent | local | 12 | 69,322 | 2,290 | 134s | ended without finish | no | not resolved |
| sympy__sympy-24213 | agentless | local | 2 | 9,801 | 219 | 14s | 0 of 1 samples usable | no | not resolved |

What the transcripts in each row show:

* **Agent.** Every run searched, read and tried edits through structured tool
  calls, then stopped by answering in prose instead of calling a tool. CodePilot's
  loop treats a reply with no tool call as the end of the turn (a deliberate
  design choice in `codepilot/agent/loop.py`, not changed for this model), so
  `ended without finish`. 11 `edit_file` calls were refused — a file not yet
  read, a `old` string matching nothing or 33,036 places, a nonexistent file —
  and came back as errors the model read and responded to; one edit succeeded. One run (sympy-18199) left a patch; it applied cleanly
  to a pristine checkout and was graded: the required test still failed, and
  all 113 PASS_TO_PASS tests passed.
* **Agentless.** Localisation named a file in every case (the wrong one for
  sympy-18199 and -22714). No sample was usable: sympy-18199's localised file
  (`sympy/core/basic.py`, ~23k tokens) does not fit a 16k window, so the
  request was refused as a context overflow and counted as a failure (D22);
  the others came back without valid JSON, without `search`/`replace`, or with
  a `search` string not in the file. Each reason is in the row.

## `pre-fix/`

The first run, before D24: LiteLLM's `ollama/` route emulated tool calling and
three of four agent runs ended when a tool call came back as text. Kept as the
evidence for that fix, not as a valid run of the agent arm. (Its agentless
flask sample did produce a patch: graded F2P 0/1, P2P 18/18.)

## Cost model input

`python -m codepilot.bench.estimate --results bench/results/smoke/2026-10-02-qwen2.5-7b.jsonl`
reads these rows. Means: agent 52,019 input / 2,023 output tokens per
attempt; agentless 5,845 input / 303 output tokens per call. A 7B model that
stops early and writes short replies understates what a capable model would
use — see `bench/STUDY_PLAN.md`.
