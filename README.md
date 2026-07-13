# Autonomous SWE Agent

A production software engineering agent benchmarked on **SWE-bench-lite (300 real GitHub issues)**. Implements and compares two architectures — an **agentic tool-use loop** and a **3-phase agentless pipeline** — on the same benchmark with full cost and performance accounting.

Grounded in:
- [Anthropic's SWE-bench 49% system](https://www.anthropic.com/engineering/swe-bench-sonnet) (tool-use agent with persistent bash + str_replace editor)
- [Agentless (Xia et al., UIUC 2024)](https://arxiv.org/abs/2407.01489) (localize → repair → validate, 32% at $0.70/issue)
- [SWE-agent (Princeton 2024)](https://arxiv.org/abs/2405.15793) (Agent-Computer Interface design)

---

## Benchmark Results

> Run after completing the full SWE-bench-lite evaluation. Update this table with your results.

| Metric | Agent | Agentless |
|---|---|---|
| % Resolved | — | — |
| Resolved / Total | — / 300 | — / 300 |
| Avg Cost / Issue | — | — |
| Total Cost | — | — |
| Avg Turns | — | — (3 phases) |
| Model | BYOK (any provider) | BYOK (any provider) |

*Run the evaluation (bring your own key): `python -m eval.run_eval --compare --provider anthropic`*

---

## Architecture

### Agentic Approach

A single agent runs in a free-form loop with three tools, directly mirroring Anthropic's published SWE-bench setup:

```
Issue → [bash tool | str_replace_editor | search_codebase] × N turns → <DONE>
```

**Key design decisions:**
- **No LangGraph, no orchestration framework** — raw Anthropic SDK. The model drives the loop.
- **Persistent bash session** — shell state (cwd, env vars) persists across tool calls
- **ACI (Agent-Computer Interface)** — tool descriptions are engineered to prevent common LLM mistakes (absolute paths, output truncation, str_replace uniqueness)
- **Context window budget manager** — compresses old tool call/result pairs when approaching 80% of the 200k context budget
- **OpenTelemetry tracing** — every LLM call and tool execution is a traced span

**Tools:**

| Tool | Description |
|---|---|
| `bash` | Persistent stateful shell. Exit codes always returned. Output truncated at 8k chars. |
| `str_replace_editor` | View files (with line numbers), create files, str_replace (unique match required), insert, undo |
| `search_codebase` | BM25 + sentence-transformer hybrid search over repo files. Use before reading. |

### Agentless Approach

A deterministic 3-phase pipeline with no tool use:

```
Phase 1 — Localize: repo map → LLM identifies suspect files/functions
Phase 2 — Repair:   read suspect file → sample 10 patch candidates (temperature=1.0)
Phase 3 — Validate: apply each patch → run test suite → rank by pass rate → submit best
```

**Key properties:**
- LLM never uses tools — reads file content in the prompt
- Samples N=10 patches per location (diversity via temperature)
- Selects winner by test execution (not LLM judgment)
- Much cheaper than the agentic approach per issue

---

## Project Structure

```
Autonomous-SWE-Agent/
├── agent/                    # Agentic approach
│   ├── loop.py               # Core agent loop (raw Anthropic SDK)
│   ├── prompts.py            # System prompt + ACI design
│   ├── context.py            # Context window budget manager
│   └── tools/
│       ├── bash.py           # Persistent stateful bash tool
│       ├── editor.py         # str_replace_editor
│       └── search.py         # BM25 + embedding hybrid search
│
├── agentless/                # Agentless approach (3-phase)
│   ├── localize.py           # Phase 1: repo map + fault localization
│   ├── repair.py             # Phase 2: N patch candidates
│   ├── validate.py           # Phase 3: test execution + ranking
│   └── pipeline.py           # Orchestrates all 3 phases
│
├── sandbox/
│   ├── docker_workspace.py   # Per-task Docker container (isolated)
│   └── Dockerfile.sandbox    # Sandbox image (Python 3.11 + dev tools)
│
├── eval/
│   ├── harness.py            # SWE-bench-lite evaluation runner
│   └── run_eval.py           # CLI: python -m eval.run_eval --compare
│
├── github_integration/
│   ├── issue_fetcher.py      # Fetch GitHub issue by URL
│   └── pr_creator.py         # Open PR with agent's changes
│
├── observability/
│   ├── tracing.py            # OpenTelemetry spans (Jaeger)
│   └── metrics.py            # Prometheus: resolve rate, cost, latency
│
├── agent/
│   ├── llm.py                # Provider-agnostic LLM client (LiteLLM, BYOK)
│   └── providers.py          # Provider + model registry (Anthropic/OpenAI/Google/Groq)
├── api/main.py               # FastAPI + WebSocket streaming (BYOK, no server key)
└── tests/                    # Unit tests (mocked, no Docker/LLM needed)
```

---

## Quickstart

### 1. Install

```bash
git clone https://github.com/shiva-shivanibokka/Autonomous-SWE-Agent
cd Autonomous-SWE-Agent
pip install -e ".[dev]"
cp .env.example .env
# Bring your own key (BYOK). Set the provider key(s) you'll use for local
# eval runs — ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY / GROQ_API_KEY.
# The API/UI never store a key server-side; keys arrive per request.
```

### 2. Build the sandbox image

```bash
docker build -f sandbox/Dockerfile.sandbox -t swe-agent-sandbox:latest sandbox/
```

### 3. Run on a single GitHub issue

```python
from dotenv import load_dotenv
load_dotenv()

from sandbox.docker_workspace import DockerWorkspace
from agent.loop import run_agent

with DockerWorkspace.create(
    repo_url="https://github.com/scikit-learn/scikit-learn.git",
    commit_sha="<base_commit_sha>",
) as workspace:
    for event in run_agent(workspace, issue_text="..."):
        print(event.type, event.data)
```

### 4. Run the full evaluation

```bash
# Quick test (10 instances, ~$5-10)
python -m eval.run_eval --compare --limit 10

# Full SWE-bench-lite (300 instances)
python -m eval.run_eval --compare

# Agent only, on a different provider (BYOK)
python -m eval.run_eval --approach agent --limit 50 --provider openai --model gpt-5.6-terra

# Agentless only
python -m eval.run_eval --approach agentless --limit 50 --provider groq
```

Providers and models: `anthropic`, `openai`, `google`, `groq` — see `agent/providers.py`.

### 5. Start the API + observability

```bash
docker-compose up
# API:        http://localhost:8000   (live runs enabled locally; BYOK)
# Jaeger UI:  http://localhost:16686
# Prometheus: http://localhost:9091
```

The frontend is a separate Next.js app deployed to Vercel (see `frontend/`), which
talks to this API. Live agent runs need the Docker sandbox, so they run locally
(`docker-compose`, `ENABLE_LIVE_RUNS=true`); the hosted demo replays recorded runs.

---

## Deployment

The frontend and backend deploy separately. Live agent runs stay local (they need
a Docker sandbox); the hosted site is a BYOK console that replays a recorded run.

| Piece | Where | How |
|---|---|---|
| **Frontend** (`frontend/`) | Vercel | Git-connected. Root directory `frontend`. Set `NEXT_PUBLIC_API_BASE` to the backend URL (or leave blank for replay-only). |
| **Backend** (serving API) | Render | `render.yaml` blueprint — installs `requirements-serve.txt` (light, no PyTorch), runs `uvicorn api.main:app`. Live runs off. |
| **Full stack** (live runs, eval, tracing) | Local | `docker-compose up` — API + Jaeger + Prometheus, `ENABLE_LIVE_RUNS=true`. |

After the backend is up, set `NEXT_PUBLIC_API_BASE` on Vercel and `PUBLIC_BASE_URL`
+ `FRONTEND_ORIGIN` on Render so the browser gets a working `wss://` URL and CORS passes.

## What's Production-Grade Here

| Feature | Why It Matters |
|---|---|
| **SWE-bench evaluation** | Standardised benchmark — actual numbers, not a demo |
| **Two-approach comparison** | Agent vs. agentless on the same 300 issues — research-quality |
| **ACI tool design** | Tool descriptions engineered to prevent LLM mistakes (Anthropic's key insight) |
| **Context budget manager** | Prevents silent failure at context limit — most agents don't handle this |
| **Docker-per-task isolation** | No state bleed between tasks — production-safe |
| **OpenTelemetry tracing** | Every span visible in Jaeger — full execution tree |
| **Prometheus metrics** | Resolve rate, cost/issue, token usage — all tracked |
| **GitHub Actions CI** | Runs 5-issue smoke eval on every push to main |
| **Per-call cost accounting** | Input/output tokens × price tracked per turn |

---

## References

- [SWE-bench (Princeton, 2024)](https://arxiv.org/abs/2310.06770)
- [SWE-agent: Agent-Computer Interfaces (Princeton, 2024)](https://arxiv.org/abs/2405.15793)
- [Agentless (UIUC, 2024)](https://arxiv.org/abs/2407.01489)
- [Anthropic: Raising the bar on SWE-bench Verified](https://www.anthropic.com/engineering/swe-bench-sonnet)
- [mini-SWE-agent: 65% in 100 lines](https://github.com/SWE-agent/mini-swe-agent)
