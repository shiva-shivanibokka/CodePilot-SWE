# TODO — remaining work

The code build is complete. What's left needs **your accounts, keys, or compute** —
it can't be finished from the repo alone. Roughly in priority order.

## 1. Hosted API — DONE (no separate host needed)
The read-only endpoints the live site uses (`/providers`, `/benchmark`) are served
by Next.js route handlers inside the Vercel app — free, no separate backend host to
provision or pay for. Nothing to do here. Live agent runs still run locally (they
need a Docker sandbox); the hosted demo is replay-only by design.

Optional: if you ever want the full Python API online (e.g. to enable hosted live
runs), `Dockerfile.serve` is a light image you can deploy to any container host.

## 2. Confirm the Vercel root directory — ~1 min
- [ ] Vercel → project → Settings → Build & Deployment → **Root Directory = `frontend`**.
      It should already be set (the project was linked from `frontend/`), but a wrong
      value here is the one thing that breaks a git-triggered build.

## 3. Generate real benchmark numbers — needs Docker + a key + $
The benchmark table stays empty until a real run exists. No numbers are ever fabricated.
- [ ] `docker build -f sandbox/Dockerfile.sandbox -t swe-agent-sandbox:latest sandbox/`
- [ ] `pip install -e ".[dev]"` then set a provider key in `.env`.
- [ ] `python -m eval.run_eval --compare --limit 10 --provider anthropic`  (~$5–10 for 10 issues)
- [ ] Paste the two summary objects into `frontend/data/benchmark.json` (and the README
      table). Push — the live site's benchmark section fills in automatically.

## 4. Record a real demo run — needs Docker + a key
Replaces the bundled sample trace in the console with a genuine run.
- [ ] `python -m eval.record_run --issue <github-issue-url> --provider anthropic`
- [ ] Import the resulting `frontend/lib/recorded-run.json` in `frontend/lib/replay.ts`
      in place of `SAMPLE_RUN`.

## 5. Optional / nice-to-have
- [ ] Add `ANTHROPIC_API_KEY` (or another provider key) as a GitHub Actions **secret**
      so the `smoke-eval` CI job actually runs on push to `main`. Without it that job
      no-ops (it won't fail the build — the eval step is `continue-on-error`).
- [ ] Custom domain on Vercel (Settings → Domains).
- [ ] Test coverage for the agentless pipeline and the Docker sandbox (currently the
      agent loop, provider layer, tools, and context manager are covered; these two
      are integration-heavy and were deferred).

---
_Audit findings addressed in the build: broken build backend, Docker-in-cloud
deploy strategy, BYOK (no server key / no abuse surface), single-worker task store,
`wss://`, scoped CORS, metrics error accounting, Gradio removal, missing LICENSE,
pydantic deprecations, and core-loop test coverage._
