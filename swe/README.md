# Autonomous SWE Agent

Takes a GitHub issue, writes a patch inside an isolated workspace, and runs the
repository's own tests against it — built two ways, an **agentic tool-use loop**
and a **tool-free agentless pipeline**, so the same task can be run through both
and compared with every token, turn and dollar accounted for.

[![CI](https://github.com/shiva-shivanibokka/Autonomous-SWE-Agent/actions/workflows/ci.yml/badge.svg)](https://github.com/shiva-shivanibokka/Autonomous-SWE-Agent/actions/workflows/ci.yml)
![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)
![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)

**[View the recorded run →](https://autonomous-swe-agent.vercel.app)**

> ### This is a replay, not a live service
>
> The page above plays back **eight real runs** — both architectures on the
> same four issues — captured with `python -m eval.record_run` and committed to
> this repository. Pick one and watch it. Every line of them is measured: the model's own reasoning, the tool calls it chose, the token
> counts and dollar cost the provider billed, the patch it produced, and the
> result of running the benchmark's own tests against that patch afterwards.
> Only the pacing is synthetic: the gaps where the model was thinking are
> clamped, so a run of a few minutes replays in about half of one.
>
> **There is no hosted backend.** A run needs several minutes, a checkout of a
> third-party repository, and a shell to execute code that nobody has vetted.
> No free tier offers that, and putting a "Run" button on a public page that
> executes arbitrary repositories with someone else's key would be a bad idea
> even if one did. So the honest options were a dead button or a real recording,
> and this is the recording.
>
> **You can run it live in two commands** — see
> [Run it yourself](#run-it-yourself). No Docker required.

The site has five sections: **What this is** (the project and, more usefully,
what the recordings do *not* support), **Watch it work** (pick a run and watch
it, with its patch and grading beside the log), **Benchmark** (including the
recorded head-to-head between the two architectures), **Architecture**, and
**Run it yourself**.

---

## What the recorded runs show

Four SWE-bench-lite instances, each run end to end by **both** architectures and
graded against its own tests. Difficulty is **SWE-bench Verified's human
annotation** — how long its annotators judged the issue would take an engineer —
not anything estimated here. Instances outside Verified are shown unrated rather
than guessed at.

| Difficulty | Instance | Agentic loop | Agentless pipeline |
|---|---|---|---|
| `<15 min fix` | `sympy__sympy-22714` | **Resolved** · $0.137 · 169s | **Resolved** · $0.307 · 262s |
| `15 min – 1 hour` | `sympy__sympy-24213` | **Resolved** · $0.086 · 38s | **Resolved** · $0.063 · 34s |
| `1–4 hours` | `sympy__sympy-18199` | Not resolved · $0.071 · 54s | Not resolved · $0.190 · 131s |
| unrated | `pallets__flask-4992` | **Resolved** · $0.111 · 30s | Not resolved · $0.091 · 34s |

**Agentic: 3 of 4. Agentless: 2 of 4.**
$1.06 for the set, `claude-sonnet-5` throughout.

Four issues cannot rank two architectures, and this is not offered as a ranking.
What it does show is where they diverge, and the divergence is not the one the
cost tables in either paper would lead you to expect.

### The three most interesting ones

**`flask-4992` — the loop's advantage, in one instance.** The issue proposes
`mode="b"`. The API Flask actually shipped, and therefore the API the graded
test calls, is `text=False`. The agentic run read the surrounding code, chose
`text: bool = True`, updated the docstring from `toml` to `tomllib`, added the
`versionchanged` note, then ran the suite and used `git stash` to confirm the
remaining failures were pre-existing. The agentless pipeline has no shell and
cannot look around: it implemented what the issue asked for, which is the wrong
API, and failed. That is the architectural trade-off stated plainly — a pipeline
is cheaper and more predictable right up until the issue is wrong.

**`sympy-24213` — the pipeline's advantage, in one instance.** The fault is one
function, the issue names it precisely, and there is nothing to explore. The
pipeline localized it in a single call and sampled four patches against it; the
loop spent seven turns arriving at the same two-line edit. Same patch, less
money, fewer moving parts.

**`sympy-18199` — where both of them stop.** The issue says `nthroot_mod` misses
the root `x = 0` when `a % p == 0`. Both arms implemented exactly that, and both
are a correct reading of the issue. The fix the maintainers shipped changed
**49 lines**: it also adds `_nthroot_mod_composite`, support for **composite
moduli** that the issue never mentions. The graded test exercises `solveset` over
`Mod(x**3, 8)`, and 8 is composite — so neither patch can pass it.

Both failures are on the site, with their diffs and their test output, and
neither was re-run to get a better one. An agent that solves easy issues and
visibly breaks on hard ones is a more useful artifact than staged successes:
"where does it break" is the first thing anyone reading this will want to know.

## What it does

Given a GitHub issue, produce a patch that makes the previously-failing tests
pass without breaking the passing ones — the official SWE-bench criterion. Two
implementations share a workspace and an LLM client and nothing else:

- **Agentic** — one model drives a free-form loop with three tools (bash, a file
  editor, hybrid code search) until it declares itself done. Mirrors Anthropic's
  published SWE-bench setup.
- **Agentless** — a deterministic pipeline: localize the fault → sample N
  candidate patches → run tests and keep the best. No tool use at all. Mirrors
  the UIUC *Agentless* paper.

The point is the comparison and the legibility. Two very different architectures,
identical inputs, and every internal — cost, tokens, turns, tool calls, traces —
visible rather than hidden behind a framework.

Grounded in: [Anthropic's SWE-bench work](https://www.anthropic.com/engineering/swe-bench-sonnet),
[Agentless (Xia et al., 2024)](https://arxiv.org/abs/2407.01489),
[SWE-agent (Princeton, 2024)](https://arxiv.org/abs/2405.15793).

## Status — what is and is not measured

| Verified | Not done |
|---|---|
| Both architectures implemented and runnable | **No full 300-instance benchmark has been run** — the results table is empty on purpose |
| Both arms recorded end to end on the same four issues, graded and published | Four issues is not a resolve rate, and both arms on one issue is still one issue |
| Difficulty labelled from SWE-bench Verified's human annotations | In-harness grading is a capped proxy, not the official grader |
| Two workspace backends: Docker sandbox, and local (no Docker) | No integration tests for the Docker backend (Docker-gated) |
| BYOK across four providers through one client | |
| Hard timeouts, per-task isolation, secret scanning on recordings | |
| 93 unit tests, ruff clean, CI green | |

Any resolve-rate figures you see referenced elsewhere (Anthropic's ~49%, Agentless'
~32%) belong to **those papers**, not to this system. Nothing here is extrapolated
from them.

## Run it yourself

Two paths. Pick based on whether you have Docker.

### Without Docker — the fast path

The agent runs against a checkout in a temp directory on your machine.

```bash
git clone https://github.com/shiva-shivanibokka/Autonomous-SWE-Agent
cd Autonomous-SWE-Agent
pip install -e ".[dev]"
cp .env.example .env        # add one provider key: ANTHROPIC_API_KEY, OPENAI_API_KEY, …
```

```bash
python -m eval.record_run \
  --instance pallets__flask-4992 --backend local \
  --setup 'python -m pip install -q -r requirements/tests.txt && python -m pip install -q "pytest>=7.4,<8" "werkzeug<2.3"' \
  --pytest-args '-W ignore::DeprecationWarning'
```

That clones the repository at the exact commit the issue was filed against,
gives it its own virtualenv, hands the issue to the agent, and grades the
resulting patch against the instance's own FAIL_TO_PASS and PASS_TO_PASS tests.
The recording lands in `frontend/public/demo/run.json`, which the frontend picks
up automatically.

> ⚠️ **The local backend is not a sandbox.** The model's shell commands run as
> you, on your machine, with your network and your filesystem. There is a
> refusal list for the obviously catastrophic commands and a hard timeout on
> every command, but that narrows the blast radius rather than removing it.
> Point it at repositories you already trust; use Docker for anything else.

### With Docker — the isolated path

One throwaway container per task, `network_mode=none`, non-root, memory- and
CPU-capped, host filesystem never mounted. This is the right backend for a
benchmark over 300 arbitrary repositories.

```bash
docker build -f sandbox/Dockerfile.sandbox -t swe-agent-sandbox:latest sandbox/
export WORKSPACE_BACKEND=docker          # the default
python -m eval.record_run --instance pallets__flask-4992
```

Or bring up the whole local stack — API, Jaeger, Prometheus:

```bash
docker-compose up
# API        http://localhost:8000
# Jaeger UI  http://localhost:16686
# Prometheus http://localhost:9091
```

### Run the benchmark

```bash
pip install -e ".[eval]"                                   # SWE-bench dataset
python -m eval.run_eval --compare --limit 10               # 20 runs; the one measured run cost $0.11
python -m eval.run_eval --approach agent --limit 50 --provider openai
```

Results write to `eval/results/`. Paste the two summary objects into
`frontend/data/benchmark.json` and the site's benchmark table fills itself in.

### Run the frontend

```bash
cd frontend && npm install && npm run dev      # http://localhost:3000
# For live runs against a local backend:
#   NEXT_PUBLIC_API_BASE=http://localhost:8000
```

## Architecture

```mermaid
flowchart TD
    B["Browser"] -->|HTTPS| V["Vercel: Next.js frontend<br/>plays the recorded run<br/>+ /api/providers, /api/benchmark"]
    V -.->|"live runs (local only)"| API["FastAPI backend<br/>POST /tasks · WS /ws/:id"]
    API --> WS["create_workspace()"]
    WS --> D["DockerWorkspace<br/>one network-less container per task"]
    WS --> L["LocalWorkspace<br/>temp dir + per-task venv, no isolation"]
    D --> AG{"approach?"}
    L --> AG
    AG -->|agentic| LOOP["agent/loop.py<br/>complete() → tool_calls →<br/>bash · editor · search → &lt;DONE&gt;"]
    AG -->|agentless| P["agentless/pipeline.py<br/>localize → repair ×N → validate"]
    LOOP --> LLM["agent/llm.py (LiteLLM, BYOK)<br/>Anthropic · OpenAI · Google · Groq"]
    P --> LLM
    LOOP --> OBS["OpenTelemetry spans + Prometheus metrics"]
    P --> OBS
    LOOP --> R["TaskResult: diff, cost, tokens, turns"]
    P --> R
```

**Why this shape.** The **workspace is the trust boundary** — the model issues
arbitrary shell commands against arbitrary third-party code, so execution is
confined and one-per-task guarantees no state bleed between benchmark instances.
Making it an interface rather than a class is what lets the project run on a
machine with no Docker: same loop, same tools, different backend, and the model
cannot tell which one it has. The **LLM client is the provider seam** — one
normalized `complete()` contract means the same loop runs on four providers with
the caller's own key. The **agentic and agentless paths share only those two
things**, so the two experiments stay cleanly separable.

## Tech Stack

| Layer | Choice | Why |
|---|---|---|
| LLM access | **LiteLLM** | One OpenAI-shaped interface (tool calling + cost) across four providers — avoids four fragile SDK adapters. |
| Backend | **FastAPI + Uvicorn** | Async ASGI with first-class WebSockets for streaming agent events; Pydantic validation. |
| Sandbox | **Docker SDK** | OS-level isolation for untrusted code; one container per task. |
| Fallback backend | **subprocess + venv** | Same contract, no daemon — the project stays runnable without Docker. |
| Search | **rank-bm25 + NumPy** (+ optional MiniLM) | Lexical retrieval always; semantic half is an extra, since it costs ~2.5 GB of PyTorch. |
| Tokens | **tiktoken** | Fast estimate to trigger context compression before the window runs out. |
| Observability | **OpenTelemetry + Prometheus** | Traces answer "what did this run do"; metrics answer "resolve rate / cost / latency". |
| Eval | **SWE-bench-lite** | 300 real issues with FAIL_TO_PASS / PASS_TO_PASS grading. Loaded over HTTP so the heavy package is optional. |
| Frontend | **Next.js (App Router) on Vercel** | Static page plus two read-only route handlers — no second host to run or pay for. |

## What running it found

The unit suite was green before any of this and caught none of it. Every one of
these only appears when the thing actually executes:

| Bug | Why it mattered |
|---|---|
| Repo streamed into the container as **root**, agent ran as `sweagent` | Every edit failed and `git diff` returned empty — every task would have scored zero, looking like model failure |
| `SANDBOX_CMD_TIMEOUT` was **accepted and ignored** | One hanging test suite hangs the entire run, forever |
| The timeout branch read `workspace._timeout_seconds`, which never existed | The moment a timeout did fire, `AttributeError` instead of a message to the model |
| `subprocess` timeout killed only the shell, not its children | A 3-second deadline took 30 seconds to return |
| Pytest summary parsed `"2 failed, 5 passed"` as **5 passed, 0 failed** | The agentless validator crowned patches that broke the tests |
| Agentless asked for a whole rewritten file inside a JSON string | Any file over the token cap truncated → unparseable → candidate silently dropped |
| Eval never applied the instance's `test_patch` | The graded tests did not exist in the checkout; both approaches would have reported 0% |
| `FAIL_TO_PASS` ids passed to pytest positionally | django and sympy write bare test names, not node ids — pytest exits 4, indistinguishable from a failure. **191 of the 300 instances could only ever score zero** |
| Agentless validated candidates against the **graded** tests | Marking its own homework |
| Search index cached per task, built with the first call's `file_pattern` | Later searches silently answered from the wrong corpus |
| Agent loop caught only `LLMError` | Any tool or sandbox failure escaped the generator, discarding the diff, cost and event log |
| Undo buffer keyed by path alone | Concurrent eval workers restored each other's files |
| `git diff HEAD` omitted untracked files | A patch that adds a new module read as "changed nothing" |
| Shallow clone + checkout of a years-old commit | Most SWE-bench instances could not be checked out at all |

Recording the **agentless** arm meant running it end to end for the first time,
and it had the same story — five bugs that every unit test passed straight over:

| Bug | Why it mattered |
|---|---|
| A candidate was kept only if the suite came back **completely clean** | SWE-bench checks out a years-old commit and installs current dependencies over it, so some tests are red before the model touches anything. On flask this discarded all four candidates — including working ones — and reported it as the model failing to write a patch. The gate now measures the suite *before* patching and asks the only question that means anything: did this candidate break something that worked? |
| Validation ran the repository's **entire** test suite, once per candidate | Sympy's full suite is the better part of an hour; ten candidates is ten of those. This is why the arm had only ever been unit-tested — nobody could afford to run it. It now runs the tests nearest the patched file |
| Localization returned the same function twice, and the sample budget was split across the duplicate | Half of every run was paid to ask an identical question about an identical file |
| Rejected samples were reported only when **every** sample failed | A run that silently discarded half of what it paid for looked identical to one that discarded none |
| Repair capped responses at 2048 tokens | On a long function the reply ran past the cap and arrived as unclosed JSON — counted as a rejected sample, and billed like a good one |

The frontend had its own version of the same problem: the console shipped a
hand-written `SAMPLE_RUN` with invented reasoning, invented token counts and an
invented `4 passed in 3.21s`, streaming into the UI looking exactly like a real
run. It is now a real run or nothing — `frontend/scripts/validate-run.mjs` fails
the build if a recording is missing, malformed, oversized, or contains anything
key-shaped.

And its own set of bugs, most of which only appear in a browser rather than in a
build:

| Bug | Why it mattered |
|---|---|
| `goldPatchLines` was still `patch.count("\n")` in the recorder | The corrected added-plus-removed figures had only ever been fixed *in the committed data*, never in the script that writes it — and `agentPatchLines` was never written by the recorder at all. The next recording would have silently reintroduced the bug below |
| The done event reported `len(diff.splitlines())` as "lines changed" | Headers, hunk markers and context all counted: a one-line edit read as **13 lines changed** while the patch view beside it correctly showed one |
| `.meta` styled both the rail's key/value list and the diff's header rows | Equal specificity, so every diff header inherited `flex-direction: column` and stood on end at triple height |
| `cache: "force-cache"` on the recordings | A returning visitor kept the old file **indefinitely** — the site served superseded numbers with nothing to signal it |
| The `hashchange` handler read only the section, never the run | `#run/<id>` worked on a cold load and was ignored on back, forward, and every link followed after the first |
| `.hero` and `.panel` also carry `.wrap`; their `padding` shorthand won | The page gutters silently resolved to zero, so the tab strip and the content sat on different left edges |
| Timers chained per event | Chrome throttles background tabs, so switching away froze playback mid-run; scheduling against a wall clock flushes the backlog instead |

## Benchmark Results

**No full 300-instance run exists yet, and this table is empty rather than
estimated.** Running `eval.run_eval --compare` fills it.

| Metric | Agentic | Agentless |
|---|---|---|
| % Resolved | — | — |
| Resolved / Total | — / 300 | — / 300 |
| Avg cost / issue | — | — |
| Avg turns | — | — (3 phases) |

What *has* been measured is the four-instance head-to-head above: **3/4
against 2/4**, at $0.101 and $0.163 per issue
respectively. Four issues chosen for having environments that still build on a
modern interpreter is a biased sample and far too small to rank anything, which
is why it sits beside the empty table rather than inside it.

## Project Structure

```
Autonomous-SWE-Agent/
├── agent/
│   ├── loop.py            # Agentic loop — the model drives, tools dispatch
│   ├── llm.py             # Provider-agnostic BYOK client (LiteLLM)
│   ├── providers.py       # Provider + model registry (single source of truth)
│   ├── prompts.py         # System prompt + ACI workflow
│   ├── context.py         # Context-window budget manager / compression
│   └── tools/             # bash · str_replace_editor · search_codebase
├── agentless/             # localize → repair(×N) → validate → pipeline
├── sandbox/
│   ├── workspace.py       # The contract both backends implement
│   ├── docker_workspace.py# Container per task, network-less, non-root
│   └── local_workspace.py # Temp dir + per-task venv, no isolation
├── eval/
│   ├── harness.py         # SWE-bench-lite loader, grading, aggregation
│   ├── run_eval.py        # Benchmark CLI
│   └── record_run.py      # Records the run the frontend replays
├── github_integration/    # issue_fetcher.py + pr_creator.py
├── observability/         # tracing.py (OTel) + metrics.py (Prometheus)
├── api/                   # main.py (FastAPI + WS) + schemas.py (Pydantic)
├── frontend/
│   ├── components/
│   │   ├── AppShell.tsx   # Rail + section routing, deep links to a run
│   │   ├── Rail.tsx       # Sections, loaded-run identity, what is measured
│   │   ├── RunDeck.tsx    # The split: facts and patch left, log right
│   │   ├── RunPicker.tsx  # Choose a recording, by difficulty and outcome
│   │   ├── About.tsx      # What this is, and what it does not show
│   │   └── InfoTip.tsx    # The "?" beside a figure
│   ├── public/demo/       # The recordings: index.json + one file per run
│   └── scripts/           # validate-run.mjs — fails the build on a bad recording
└── tests/                 # 85 unit tests — mocked, no Docker/LLM/network
```

## Testing

```bash
pytest tests/          # 85 tests, fully mocked
ruff check .           # uses the rule set in pyproject.toml
```

Coverage is partial and stated as such: the provider layer, the agent loop's
control flow (mocked model), the three tools, the context manager, the local
backend's path translation, and a regression test for each bug listed above.
There are **no integration tests** for the Docker backend, and no end-to-end
test of a real agent run — that is what `eval.record_run` is for, and it costs
money to run.

## Known limitations

- **Grading is a proxy.** `eval/harness.py` applies the instance's test patch and
  runs the FAIL_TO_PASS / PASS_TO_PASS ids it names, capped at 20 tests. The
  official SWE-bench grader re-runs the full sets inside their own per-instance
  images. Numbers from this harness are indicative.
- **Environment pinning is manual.** SWE-bench instances are years old and often
  need dependency pins a modern interpreter would not choose. `--setup` takes
  that command and records it with the run; there is no per-instance environment
  database.
- **The agentless arm is unrecorded.** It runs, and its parts are tested, but no
  captured end-to-end run of it is published yet.
- **Four instances is not a resolve rate.** Three of four resolved, but they were
  chosen for having environments that build on a modern interpreter, which is
  its own selection bias. Nothing here should be read as a score over 300.
- **Single worker.** The API's task store is in-process. Scaling out needs Redis
  before raising the worker count.

## License

[MIT](LICENSE) © 2026 Shivani Bokka

## References

- [SWE-bench (Princeton, 2024)](https://arxiv.org/abs/2310.06770)
- [SWE-agent: Agent-Computer Interfaces (Princeton, 2024)](https://arxiv.org/abs/2405.15793)
- [Agentless (UIUC, 2024)](https://arxiv.org/abs/2407.01489)
- [Anthropic: Raising the bar on SWE-bench Verified](https://www.anthropic.com/engineering/swe-bench-sonnet)

---

Built by **Shivani Bokka**
