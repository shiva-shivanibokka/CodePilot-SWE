"""
Pydantic request/response schemas for the FastAPI endpoints.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Approach(str, Enum):
    AGENT = "agent"
    AGENTLESS = "agentless"


class TaskRequest(BaseModel):
    """Request body for starting a new task."""

    issue_url: str | None = Field(
        None,
        description="GitHub issue URL (e.g. https://github.com/owner/repo/issues/123). "
        "Provide either this or issue_text + repo_url.",
        json_schema_extra={"example": "https://github.com/scikit-learn/scikit-learn/issues/12462"},
    )
    issue_text: str | None = Field(
        None,
        description="Raw issue text (title + body). Used when issue_url is not provided.",
    )
    repo_url: str | None = Field(
        None,
        description="HTTPS Git clone URL (required when using issue_text).",
        json_schema_extra={"example": "https://github.com/scikit-learn/scikit-learn.git"},
    )
    commit_sha: str | None = Field(
        None,
        description="Commit SHA to check out. Defaults to HEAD of default branch.",
    )
    approach: Approach = Field(
        Approach.AGENT,
        description="Which approach to use: 'agent' (tool-use loop) or 'agentless' (3-phase).",
    )
    provider: str = Field(
        ...,
        description="LLM provider (BYOK): anthropic | openai | google | groq.",
    )
    model: str = Field(..., description="Model id for the chosen provider.")
    api_key: str = Field(
        ...,
        description="Your own API key for the chosen provider (BYOK). Used for this "
        "request only — never stored or logged.",
    )
    create_pr: bool = Field(
        False,
        description="If True, open a GitHub PR with the changes after the task completes.",
    )

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, v: str) -> str:
        from agent.providers import PROVIDERS

        if v not in PROVIDERS:
            raise ValueError(f"Unknown provider {v!r}. Options: {sorted(PROVIDERS)}")
        return v

    def model_post_init(self, __context: Any) -> None:
        if not self.issue_url and not (self.issue_text and self.repo_url):
            raise ValueError("Provide either issue_url, or both issue_text and repo_url.")


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskResponse(BaseModel):
    """Response after creating a task."""

    task_id: str
    status: TaskStatus
    websocket_url: str
    message: str = "Task started. Connect to websocket_url for live events."


class TaskResult(BaseModel):
    """Final task result returned via REST or WebSocket DONE event."""

    task_id: str
    resolved: bool
    conclusion: str
    diff: str
    turns: int
    cost_usd: float
    input_tokens: int
    output_tokens: int
    duration_seconds: float
    stop_reason: str
    pr_url: str | None = None
    error: str | None = None


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "0.1.0"
