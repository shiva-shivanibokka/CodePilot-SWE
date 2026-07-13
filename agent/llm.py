"""
Provider-agnostic LLM client (BYOK) built on LiteLLM.

One call surface for Anthropic / OpenAI / Google / Groq. Callers pass an
LLMConfig carrying the provider key, model id, and the end-user's own API key
(BYOK) — nothing is read from process env, nothing is stored, nothing is logged.

Messages use the OpenAI / LiteLLM format (LiteLLM normalizes every provider to it):
    {"role": "system"|"user"|"assistant"|"tool", "content": str, ...}
Assistant tool calls:  msg["tool_calls"] = [{"id","type":"function","function":{...}}]
Tool results:          {"role": "tool", "tool_call_id": id, "content": str}
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agent.providers import litellm_model

# litellm is imported lazily inside complete() so this module (LLMConfig, the
# tool converter, the dataclasses) imports without the heavy dep present — keeps
# imports light and lets the agent loop be unit-tested with a mocked complete().
_litellm_configured = False


def _get_litellm():
    global _litellm_configured
    import litellm

    if not _litellm_configured:
        # Drop per-provider params some backends reject rather than erroring,
        # and send no usage telemetry from a user's BYOK call.
        litellm.drop_params = True
        litellm.telemetry = False
        _litellm_configured = True
    return litellm


@dataclass(frozen=True)
class LLMConfig:
    """Which provider/model to call and the caller's own API key (BYOK)."""

    provider: str
    model: str
    api_key: str


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    input_tokens: int
    output_tokens: int
    cost_usd: float
    finish_reason: str


class LLMError(Exception):
    """Provider/network/auth error surfaced from a completion call."""


def to_openai_tools(tool_schemas: list[dict]) -> list[dict]:
    """Convert the repo's Anthropic-style tool defs ({name, description, input_schema})
    into the OpenAI function-tool format LiteLLM expects for every provider."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            },
        }
        for t in tool_schemas
    ]


def complete(
    cfg: LLMConfig,
    messages: list[dict[str, Any]],
    *,
    system: str | None = None,
    tools: list[dict] | None = None,
    temperature: float = 0.2,
    max_tokens: int = 4096,
) -> LLMResponse:
    """Call the model once. Raises LLMError on any provider/auth/network failure."""
    if not cfg.api_key:
        raise LLMError("No API key provided. This is a bring-your-own-key demo.")

    msgs = list(messages)
    if system:
        msgs = [{"role": "system", "content": system}, *msgs]

    kwargs: dict[str, Any] = {
        "model": litellm_model(cfg.provider, cfg.model),
        "messages": msgs,
        "api_key": cfg.api_key,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        kwargs["tools"] = to_openai_tools(tools)

    litellm = _get_litellm()
    try:
        resp = litellm.completion(**kwargs)
    except Exception as exc:  # litellm raises provider-specific exceptions
        raise LLMError(str(exc)) from exc

    choice = resp.choices[0]
    msg = choice.message

    tool_calls: list[ToolCall] = []
    for tc in getattr(msg, "tool_calls", None) or []:
        try:
            args = json.loads(tc.function.arguments or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}
        tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))

    usage = resp.usage
    try:
        cost = litellm.completion_cost(completion_response=resp) or 0.0
    except Exception:
        cost = 0.0  # unknown/self-hosted model — cost map may not cover it

    return LLMResponse(
        text=msg.content or "",
        tool_calls=tool_calls,
        input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        cost_usd=cost,
        finish_reason=choice.finish_reason or "stop",
    )


def assistant_message(resp: LLMResponse) -> dict[str, Any]:
    """Rebuild the assistant message (with tool_calls) to append to history so the
    next turn's tool results line up with their calls."""
    msg: dict[str, Any] = {"role": "assistant", "content": resp.text or ""}
    if resp.tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
            }
            for tc in resp.tool_calls
        ]
    return msg
