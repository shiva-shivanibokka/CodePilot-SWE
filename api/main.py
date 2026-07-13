"""
FastAPI application with WebSocket streaming.

Endpoints:
    POST /tasks              — Start a new agent/agentless task
    GET  /tasks/{task_id}    — Get task status and result
    WS   /ws/{task_id}       — WebSocket for live agent events
    GET  /health             — Health check
    GET  /metrics/summary    — Current resolve rates and cost summary

The WebSocket streams AgentEvent objects as JSON as the agent works.
The final DONE event contains the full TaskResult.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

from agent.llm import LLMConfig
from agent.providers import providers_payload
from api.schemas import Approach, HealthResponse, TaskRequest, TaskResponse, TaskStatus
from observability.metrics import start_metrics_server
from observability.tracing import setup_tracing

# ── Config ───────────────────────────────────────────────────────────────────
# Live runs need a Docker sandbox to execute untrusted repo code, so they only
# work where Docker is available (locally via docker-compose). On a PaaS without
# Docker (Render/Vercel) this stays off and the hosted demo is replay-only.
ENABLE_LIVE_RUNS = os.getenv("ENABLE_LIVE_RUNS", "false").lower() == "true"
# Public origin the frontend reaches this API on, e.g. https://api.example.com.
# Used to hand back a wss:// URL that works behind TLS.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")
# CORS: the deployed frontend origin. "*" is fine for local dev only.
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "*")

# In-memory task store: task_id -> {"status", "result", "events", "queue"}
# Single-worker only (see uvicorn --workers 1); use Redis to scale out.
_tasks: dict[str, dict[str, Any]] = {}
_ws_queues: dict[str, asyncio.Queue] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown logic."""
    setup_tracing(service_name="swe-agent-api")
    start_metrics_server()
    yield
    # Cleanup on shutdown (teardown any orphaned sandboxes)


app = FastAPI(
    title="Autonomous SWE Agent API",
    description=(
        "Production SWE agent benchmarked on SWE-bench-lite. "
        "Resolves GitHub issues autonomously using an agentic tool-use loop "
        "or a 3-phase agentless approach. Streams live events via WebSocket."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN] if FRONTEND_ORIGIN != "*" else ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", response_model=HealthResponse, tags=["System"])
async def health():
    return HealthResponse()


@app.get("/providers", tags=["System"])
async def providers():
    """Provider + model registry for the BYOK dropdowns, plus which run modes
    this instance allows."""
    return {"providers": providers_payload(), "live_runs_enabled": ENABLE_LIVE_RUNS}


@app.get("/benchmark", tags=["System"])
async def benchmark():
    """Latest agent + agentless eval summaries, if any run has been recorded.
    Reads eval/results/*_summary.json — numbers are never fabricated; the
    frontend shows a 'not yet run' state when this is empty."""
    from pathlib import Path

    results_dir = Path(__file__).parent.parent / "eval" / "results"
    fields = (
        "approach",
        "model",
        "resolve_rate",
        "resolved_count",
        "total_instances",
        "avg_cost_usd",
        "total_cost_usd",
        "avg_turns",
    )
    summaries = []
    if results_dir.exists():
        for approach in ("agent", "agentless"):
            files = sorted(results_dir.glob(f"{approach}_*_summary.json"), reverse=True)
            if not files:
                continue
            try:
                data = json.loads(files[0].read_text())
                summaries.append({k: data[k] for k in fields})
            except (json.JSONDecodeError, KeyError, OSError):
                continue
    return {"summaries": summaries}


@app.get("/metrics/summary", tags=["System"])
async def metrics_summary():
    """Current resolve rates and cost totals."""
    return {
        "note": "For full Prometheus metrics, see /metrics on port 9090",
        "active_tasks": {
            "agent": len(
                [
                    t
                    for t in _tasks.values()
                    if t["status"] == "running" and t.get("approach") == "agent"
                ]
            ),
            "agentless": len(
                [
                    t
                    for t in _tasks.values()
                    if t["status"] == "running" and t.get("approach") == "agentless"
                ]
            ),
        },
        "total_tasks": len(_tasks),
    }


@app.post("/tasks", response_model=TaskResponse, status_code=202, tags=["Tasks"])
async def create_task(request: TaskRequest):
    """
    Start a new task.

    The task runs asynchronously. Connect to the WebSocket endpoint
    for live streaming of agent events. Requires ENABLE_LIVE_RUNS=true and a
    Docker-capable host (the agent sandboxes untrusted repo code).
    """
    if not ENABLE_LIVE_RUNS:
        raise HTTPException(
            status_code=503,
            detail=(
                "Live runs are disabled on this instance (no Docker sandbox available). "
                "Run locally with docker-compose (ENABLE_LIVE_RUNS=true), or use the recorded demo."
            ),
        )

    task_id = str(uuid.uuid4())[:12]
    queue: asyncio.Queue = asyncio.Queue()
    _tasks[task_id] = {
        "status": TaskStatus.PENDING,
        "approach": request.approach.value,
        "result": None,
        "events": [],
        "error": None,
    }
    _ws_queues[task_id] = queue

    # Start the task in the background
    asyncio.create_task(_run_task_background(task_id, request, queue))

    if PUBLIC_BASE_URL:
        ws_base = PUBLIC_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")
    else:
        host = os.getenv("API_HOST", "localhost")
        port = os.getenv("API_PORT", "8000")
        ws_base = f"ws://{host}:{port}"

    return TaskResponse(
        task_id=task_id,
        status=TaskStatus.PENDING,
        websocket_url=f"{ws_base}/ws/{task_id}",
    )


@app.get("/tasks/{task_id}", tags=["Tasks"])
async def get_task(task_id: str):
    """Get task status and final result (if complete)."""
    if task_id not in _tasks:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return _tasks[task_id]


@app.websocket("/ws/{task_id}")
async def websocket_endpoint(websocket: WebSocket, task_id: str):
    """
    WebSocket endpoint for live agent event streaming.

    Sends AgentEvent JSON objects as the agent works.
    Closes when the DONE event is sent.
    """
    await websocket.accept()

    if task_id not in _ws_queues:
        await websocket.send_text(
            json.dumps(
                {
                    "type": "error",
                    "data": {"error": f"Task {task_id} not found"},
                }
            )
        )
        await websocket.close()
        return

    queue = _ws_queues[task_id]

    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
            except TimeoutError:
                # Send heartbeat to keep connection alive
                await websocket.send_text(json.dumps({"type": "heartbeat"}))
                continue

            await websocket.send_text(json.dumps(event))

            if event.get("type") in ("done", "error"):
                break

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "error",
                        "data": {"error": str(exc)},
                    }
                )
            )
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
        _ws_queues.pop(task_id, None)


async def _run_task_background(
    task_id: str,
    request: TaskRequest,
    queue: asyncio.Queue,
) -> None:
    """
    Background coroutine that runs the agent/agentless pipeline
    and pushes events to the WebSocket queue.
    """
    _tasks[task_id]["status"] = TaskStatus.RUNNING

    # BYOK: the caller's own provider/model/key, used for this request only.
    llm = LLMConfig(provider=request.provider, model=request.model, api_key=request.api_key)

    try:
        # Resolve issue data
        if request.issue_url:
            from github_integration.issue_fetcher import fetch_issue

            issue_data = await asyncio.get_event_loop().run_in_executor(
                None, fetch_issue, request.issue_url
            )
            issue_text = issue_data.issue_text
            repo_url = issue_data.repo_url
            commit_sha = issue_data.base_commit
        else:
            issue_text = request.issue_text
            repo_url = request.repo_url
            commit_sha = request.commit_sha or "HEAD"

        # Run in thread pool (Docker ops are blocking)
        loop = asyncio.get_event_loop()

        if request.approach == Approach.AGENT:
            result = await loop.run_in_executor(
                None,
                _run_agent_sync,
                task_id,
                issue_text,
                repo_url,
                commit_sha,
                llm,
                queue,
                loop,
            )
        else:
            result = await loop.run_in_executor(
                None,
                _run_agentless_sync,
                task_id,
                issue_text,
                repo_url,
                commit_sha,
                llm,
                queue,
                loop,
            )

        _tasks[task_id]["status"] = TaskStatus.COMPLETED
        _tasks[task_id]["result"] = result

    except Exception as exc:
        error_event = {"type": "error", "data": {"error": str(exc)}, "turn": 0}
        _tasks[task_id]["status"] = TaskStatus.FAILED
        _tasks[task_id]["error"] = str(exc)
        asyncio.run_coroutine_threadsafe(queue.put(error_event), asyncio.get_event_loop())


def _run_agent_sync(task_id, issue_text, repo_url, commit_sha, llm, queue, loop):
    """Synchronous wrapper for the agent loop, called from thread pool."""
    from agent.loop import run_agent
    from sandbox.docker_workspace import DockerWorkspace

    with DockerWorkspace.create(repo_url, commit_sha, task_id=task_id[:8]) as ws:
        gen = run_agent(ws, issue_text, llm)
        task_result = None
        try:
            while True:
                event = next(gen)
                event_dict = event.to_dict()
                asyncio.run_coroutine_threadsafe(queue.put(event_dict), loop)
        except StopIteration as e:
            task_result = e.value

        return {
            "task_id": task_id,
            "resolved": task_result.resolved if task_result else False,
            "conclusion": task_result.conclusion if task_result else "",
            "diff": task_result.diff if task_result else "",
            "turns": task_result.turns if task_result else 0,
            "cost_usd": task_result.cost_usd if task_result else 0.0,
            "input_tokens": task_result.input_tokens if task_result else 0,
            "output_tokens": task_result.output_tokens if task_result else 0,
            "duration_seconds": task_result.duration_seconds if task_result else 0.0,
            "stop_reason": task_result.stop_reason if task_result else "error",
        }


def _run_agentless_sync(task_id, issue_text, repo_url, commit_sha, llm, queue, loop):
    """Synchronous wrapper for agentless pipeline, called from thread pool."""
    from agentless.pipeline import run_agentless
    from sandbox.docker_workspace import DockerWorkspace

    with DockerWorkspace.create(repo_url, commit_sha, task_id=task_id[:8]) as ws:
        # Emit phase start events
        for phase in [
            "Phase 1: Localizing...",
            "Phase 2: Generating patches...",
            "Phase 3: Validating...",
        ]:
            asyncio.run_coroutine_threadsafe(
                queue.put({"type": "thought", "data": {"text": phase}, "turn": 0}), loop
            )

        result = run_agentless(ws, issue_text, llm)

        diff = ws.get_diff()
        done_event = {
            "type": "done",
            "data": {
                "stop_reason": "done" if result.resolved else "failed",
                "resolved": result.resolved,
                "cost_usd": round(result.total_cost_usd, 5),
                "diff_lines": len(diff.splitlines()),
            },
            "turn": 0,
        }
        asyncio.run_coroutine_threadsafe(queue.put(done_event), loop)

        return {
            "task_id": task_id,
            "resolved": result.resolved,
            "conclusion": result.best_candidate.explanation if result.best_candidate else "",
            "diff": diff,
            "turns": 0,
            "cost_usd": result.total_cost_usd,
            "input_tokens": result.total_input_tokens,
            "output_tokens": result.total_output_tokens,
            "duration_seconds": 0.0,
            "stop_reason": "done" if result.resolved else "failed",
        }
