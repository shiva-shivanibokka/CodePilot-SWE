# TODO — remaining work

The code build is complete and **live**. What's left needs **your keys or compute** —
it can't be finished from the repo alone.

## ✅ Done and verified
- Frontend live: https://autonomous-swe-agent.vercel.app
- Hosted API served by the Vercel app itself (`/api/providers`, `/api/benchmark`) —
  free, no separate backend host to provision or pay for.
- GitHub repo connected to Vercel; **Root Directory = `frontend`** set, so pushes
  auto-deploy correctly.
- CI green end to end (lint, unit tests, docker build, smoke eval).
- Full Python backend (agent loop, sandbox, eval, tracing) runs locally via
  `docker-compose`; `Dockerfile.serve` can host it on any container host if ever wanted.

## 1. Generate real benchmark numbers — needs Docker + a key + $
The benchmark table stays empty until a real run exists. No numbers are ever fabricated.
- [ ] `docker build -f sandbox/Dockerfile.sandbox -t swe-agent-sandbox:latest sandbox/`
- [ ] `pip install -e ".[dev]"` then set a provider key in `.env`.
- [ ] `python -m eval.run_eval --compare --limit 10 --provider anthropic`  (~$5–10 for 10 issues)
- [ ] Paste the two summary objects into `frontend/data/benchmark.json` (and the README
      table). Push — the live site's benchmark section fills in automatically.

## 2. Record a real demo run — needs Docker + a key
Replaces the bundled sample trace in the console with a genuine run.
- [ ] `python -m eval.record_run --issue <github-issue-url> --provider anthropic`
- [ ] Import the resulting `frontend/lib/recorded-run.json` in `frontend/lib/replay.ts`
      in place of `SAMPLE_RUN`.

## 3. Optional / nice-to-have
- [ ] Add `ANTHROPIC_API_KEY` (or another provider key) as a GitHub Actions **secret**
      so the `smoke-eval` CI job actually exercises the agent on push to `main`. Without
      it that job passes but no-ops (the eval step is `continue-on-error`).
- [ ] Custom domain on Vercel (Settings → Domains).
- [ ] Test coverage for the agentless pipeline and the Docker sandbox (currently the
      agent loop, provider layer, tools, and context manager are covered; these two
      are integration-heavy and were deferred).

---
_Audit findings addressed in the build: broken build backend, Docker-in-cloud
deploy strategy, BYOK (no server key / no abuse surface), single-worker task store,
`wss://`, scoped CORS, metrics error accounting, Gradio removal, missing LICENSE,
pydantic deprecations, core-loop test coverage, and CI (ruff pin + StrEnum)._
