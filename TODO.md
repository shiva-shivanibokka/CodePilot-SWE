# TODO — remaining work

The code build is complete. What's left needs **your accounts, keys, or compute** —
it can't be finished from the repo alone. Roughly in priority order.

## 1. Deploy the backend (Render) — ~10 min
The frontend is live at https://autonomous-swe-agent.vercel.app but runs in
**replay-only** mode until a backend exists. Everything is ready:
- [ ] Render → **New → Blueprint** → point at this repo (`render.yaml` is at the root).
- [ ] After it's live, copy the service URL (e.g. `https://swe-agent-api.onrender.com`).
- [ ] Set that URL as `PUBLIC_BASE_URL` in the Render service env.
- [ ] Note: `render.yaml` installs `requirements-serve.txt` (light, no PyTorch). Live
      runs stay off on Render — that's intentional (no Docker sandbox there).

## 2. Wire the frontend to the backend — ~2 min
- [ ] Vercel → project → Settings → Environment Variables → add
      `NEXT_PUBLIC_API_BASE = <your Render URL>`.
- [ ] Redeploy the frontend (or just push — Vercel auto-deploys).
- [ ] Confirm the benchmark table + provider dropdowns now pull from the backend.

## 3. Confirm the Vercel root directory — ~1 min
- [ ] Vercel → project → Settings → Build & Deployment → **Root Directory = `frontend`**.
      It should already be set (the project was linked from `frontend/`), but a wrong
      value here is the one thing that breaks a git-triggered build.

## 4. Generate real benchmark numbers — needs Docker + a key + $
The README table and the `/benchmark` endpoint stay empty until a real run exists.
No numbers are ever fabricated.
- [ ] `docker build -f sandbox/Dockerfile.sandbox -t swe-agent-sandbox:latest sandbox/`
- [ ] `pip install -e ".[dev]"` then set a provider key in `.env`.
- [ ] `python -m eval.run_eval --compare --limit 10 --provider anthropic`  (~$5–10 for 10 issues)
- [ ] Paste the printed numbers into the README **Benchmark Results** table.

## 5. Record a real demo run — needs Docker + a key
Replaces the bundled sample trace in the console with a genuine run.
- [ ] `python -m eval.record_run --issue <github-issue-url> --provider anthropic`
- [ ] Import the resulting `frontend/lib/recorded-run.json` in `frontend/lib/replay.ts`
      in place of `SAMPLE_RUN` (or serve it from the backend and fetch it).

## 6. Optional / nice-to-have
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
