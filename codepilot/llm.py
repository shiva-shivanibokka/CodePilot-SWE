"""
The provider seam.

This is the only module in the package that talks to a model provider. It is
built on LiteLLM, so one client reaches Anthropic, OpenAI, Google Gemini and
Groq (and anything else LiteLLM routes), including the free tiers of the last
two.

Everything above this module still deals in one message shape — the
Anthropic-style content blocks the conversation, the session log and the
replay files were written around (`text`, `tool_use`, `tool_result`). The
translation to and from the OpenAI-style format LiteLLM speaks happens here and
nowhere else, so the agent loop, compaction, persistence and every test that
scripts a `Reply` are unchanged by the provider switch.

What is kept from the Anthropic-only client it replaced:

* **Cache breakpoints.** `cache_control` on the last stable system block is
  sent through to providers that honour explicit breakpoints (Anthropic) and
  stripped for the rest, which either cache implicitly (OpenAI, Gemini 2.5+,
  some Groq models) or not at all. Stripping matters: a provider that does not
  know the field may reject the request.
* **Usage and cost.** `input_tokens` is *uncached* input, as before, so
  `cache_report()` keeps meaning the same thing. LiteLLM folds cache reads and
  writes into `prompt_tokens`; they are subtracted back out here.
* **Error mapping.** A missing model, a rejected key and a forbidden model are
  `LLMError` with a message that says what to do. Rate limits and transient
  server errors are retried with backoff and surface as the provider's own
  exception if they persist, so the eval runner can tell an outage from an
  agent failure.

Cost is the *list price* LiteLLM's cost map (or the fallback table below)
assigns to the tokens. On a free tier nothing is billed; the number is what the
same run would cost on the paid tier, which is the number a study budget needs.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def load_env(root: Path | str | None = None) -> None:
    """Read `.env` from the repository being worked on, then where you ran the
    command, then `~/.codepilot.env`.

    Explicit paths, because `find_dotenv()` searches upward from the *caller*,
    which for an installed tool is site-packages. This lives beside the client
    rather than in the CLI because the CLI is not the only thing that needs a
    key: the eval harness ran a whole sweep without one and scored every task
    a failure, which is the same mistake as counting an unreachable judge's
    zero as a verdict.

    The working directory is in the list because of the ordinary case that
    used to fail: the key sits in CodePilot's own `.env`, and the repository
    being edited is somewhere else entirely.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    base = Path(root) if root is not None else Path.cwd()
    for candidate in (base / ".env", Path.cwd() / ".env", Path.home() / ".codepilot.env"):
        if candidate.is_file():
            load_dotenv(candidate, override=False)


# Overridable so a model retirement is a config change, not a code change.
# Note the asymmetry, which cost a live 404 to discover: some models are served
# under a bare alias, others only under a dated id. Any LiteLLM model string
# works here, e.g. "groq/llama-3.3-70b-versatile" or "gemini/gemini-2.5-flash".
STRONG_MODEL = os.getenv("CODEPILOT_STRONG_MODEL", "claude-opus-5")
FAST_MODEL = os.getenv("CODEPILOT_FAST_MODEL", "claude-haiku-4-5-20251001")
#: The middle rung, for asking whether routing work down beats turning the
#: effort dial down on the strong model. Not used by the agent itself.
ROUTED_MODEL = os.getenv("CODEPILOT_ROUTED_MODEL", "claude-sonnet-5")

#: USD per token, (input, output, cache_read, cache_write), keyed by the model
#: id without provider prefix or date suffix. Only consulted when LiteLLM's own
#: cost map does not know the model. The Anthropic rows are the prices the
#: original Anthropic-only client used; the Groq rows are Groq's published
#: on-demand prices and must be re-checked before a paid run — Groq models come
#: and go faster than either table is updated.
PRICING: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5": (5.00e-6, 25.00e-6, 0.50e-6, 6.25e-6),
    "claude-sonnet-5": (2.00e-6, 10.00e-6, 0.20e-6, 2.50e-6),
    "claude-haiku-4-5": (1.00e-6, 5.00e-6, 0.10e-6, 1.25e-6),
    "llama-3.3-70b-versatile": (0.59e-6, 0.79e-6, 0.59e-6, 0.59e-6),
    "llama-3.1-8b-instant": (0.05e-6, 0.08e-6, 0.05e-6, 0.05e-6),
    "meta-llama/llama-4-scout-17b-16e-instruct": (0.11e-6, 0.34e-6, 0.11e-6, 0.11e-6),
}

#: Providers that honour an explicit `cache_control` breakpoint. Everyone else
#: has it stripped before the request leaves.
EXPLICIT_CACHE_PROVIDERS = {"anthropic"}


def provider_of(model: str) -> str:
    """The LiteLLM provider a model string routes to, e.g. 'groq'."""
    try:
        import litellm

        return litellm.get_llm_provider(model)[1]
    except Exception:  # noqa: BLE001 - unknown strings fall back to the prefix
        return model.split("/", 1)[0] if "/" in model else "unknown"


def _price_key(model: str) -> str:
    bare = model.split("/", 1)[1] if model.split("/", 1)[0] in (
        "anthropic", "groq", "gemini", "openai"
    ) else model
    return re.sub(r"-\d{8}$", "", bare)


def price_of(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float | None:
    """Cost in USD from the fallback table, or None when the model has no price.

    None rather than 0.0: a cost display must be able to tell "free" apart from
    "unknown", or an unpriced model silently reads as costing nothing.
    """
    key = _price_key(model)
    if key not in PRICING:
        return None
    p_in, p_out, p_read, p_write = PRICING[key]
    return (
        input_tokens * p_in
        + output_tokens * p_out
        + cache_read_tokens * p_read
        + cache_write_tokens * p_write
    )


@dataclass
class Usage:
    #: Uncached input tokens only. Cache reads and writes are counted apart.
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )

    @property
    def prompt_tokens(self) -> int:
        """Everything the provider read for this request, cached or not."""
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


@dataclass
class Reply:
    """One assistant turn, provider-neutral."""

    text: str
    #: Content blocks in the conversation's shape (`text` / `tool_use` dicts),
    #: appended to the history verbatim so tool results line up by id.
    content: list[Any]
    tool_calls: list[ToolCall]
    stop_reason: str | None
    model: str
    usage: Usage
    latency_ms: int
    cost_usd: float | None

    @property
    def wants_tools(self) -> bool:
        return self.stop_reason == "tool_use" and bool(self.tool_calls)

    @property
    def refused(self) -> bool:
        return self.stop_reason == "refusal"


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


class LLMError(RuntimeError):
    """Raised for failures the caller cannot retry its way out of."""


# ---------------------------------------------------------------------------
# Translation: conversation shape  <->  OpenAI/LiteLLM shape
# ---------------------------------------------------------------------------


def _block(b: Any) -> dict[str, Any]:
    if isinstance(b, dict):
        return b
    if hasattr(b, "model_dump"):
        return b.model_dump(mode="json", exclude_none=True)
    return {"type": "text", "text": str(b)}


def to_openai_tools(tool_schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic-style tool defs ({name, description, input_schema}) to the
    OpenAI function-tool format LiteLLM expects for every provider."""
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


def to_openai_system(
    system: list[dict[str, Any]] | str | None, *, keep_cache_control: bool
) -> dict[str, Any] | None:
    if system is None:
        return None
    if isinstance(system, str):
        return {"role": "system", "content": system}
    blocks = [_block(b) for b in system]
    if keep_cache_control:
        parts = []
        for b in blocks:
            part: dict[str, Any] = {"type": "text", "text": b.get("text", "")}
            if "cache_control" in b:
                part["cache_control"] = b["cache_control"]
            parts.append(part)
        return {"role": "system", "content": parts}
    return {"role": "system", "content": "\n\n".join(b.get("text", "") for b in blocks)}


def _tool_result_text(block: dict[str, Any]) -> str:
    content = block.get("content", "")
    if isinstance(content, list):
        content = "\n".join(
            str(_block(c).get("text", "")) for c in content
        )
    text = str(content)
    return f"ERROR: {text}" if block.get("is_error") else text


def to_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Conversation messages to OpenAI-format messages.

    A user message carrying `tool_result` blocks becomes one `tool` message per
    result (the OpenAI shape has no multi-result message), followed by a user
    message for any text that travelled with them. Assistant `tool_use` blocks
    become `tool_calls`. Thinking blocks are dropped: they are only valid when
    echoed to the same Anthropic model, and LiteLLM carries reasoning apart.
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        role, content = m.get("role"), m.get("content")
        if isinstance(content, str) or content is None:
            out.append({"role": role, "content": content or ""})
            continue
        blocks = [_block(b) for b in content]
        if role == "assistant":
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            calls = [
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {
                        "name": b["name"],
                        "arguments": json.dumps(b.get("input") or {}),
                    },
                }
                for b in blocks
                if b.get("type") == "tool_use"
            ]
            msg: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            elif msg["content"] is None:
                msg["content"] = ""
            out.append(msg)
            continue
        texts = []
        for b in blocks:
            if b.get("type") == "tool_result":
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": b.get("tool_use_id", ""),
                        "content": _tool_result_text(b),
                    }
                )
            elif b.get("type") == "text":
                texts.append(b.get("text", ""))
        if texts:
            out.append({"role": role, "content": "\n".join(texts)})
    return out


_STOP_REASONS = {
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "refusal",
}


def _usage_of(raw: Any) -> Usage:
    """Provider usage, with input counted uncached.

    LiteLLM reports `prompt_tokens` inclusive of cache reads and writes for
    every provider it normalises (verified in litellm 1.103's Anthropic
    transformation, which adds both into prompt_tokens), so they come back out
    here to keep `input_tokens` meaning what the Anthropic SDK meant by it.
    """
    if raw is None:
        return Usage()

    def get(obj: Any, name: str) -> Any:
        if obj is None:
            return None
        return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)

    prompt = int(get(raw, "prompt_tokens") or 0)
    output = int(get(raw, "completion_tokens") or 0)
    details = get(raw, "prompt_tokens_details")
    read = int(get(raw, "cache_read_input_tokens") or get(details, "cached_tokens") or 0)
    write = int(
        get(raw, "cache_creation_input_tokens") or get(details, "cache_creation_tokens") or 0
    )
    return Usage(
        input_tokens=max(prompt - read - write, 0),
        output_tokens=output,
        cache_read_tokens=read,
        cache_write_tokens=write,
    )


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        # Handed to the tool as an unknown argument, so execute() turns it into
        # a "Bad arguments" error the model can read and correct — rather than
        # silently calling the tool with nothing.
        return {"_unparseable_arguments": str(raw)[:500]}
    return value if isinstance(value, dict) else {"_unparseable_arguments": str(raw)[:500]}


def reply_from_response(
    response: Any, *, model: str, latency_ms: int, cost_usd: float | None
) -> Reply:
    """A LiteLLM ModelResponse as a provider-neutral Reply."""
    choice = response.choices[0]
    message = choice.message
    text = getattr(message, "content", None) or ""
    if not isinstance(text, str):
        text = "".join(str(_block(p).get("text", "")) for p in text)
    tool_calls = []
    for tc in getattr(message, "tool_calls", None) or []:
        fn = tc.function if not isinstance(tc, dict) else tc["function"]
        name = fn.name if not isinstance(fn, dict) else fn["name"]
        args = fn.arguments if not isinstance(fn, dict) else fn.get("arguments")
        call_id = (tc.id if not isinstance(tc, dict) else tc.get("id")) or (
            f"call_{uuid.uuid4().hex[:12]}"
        )
        tool_calls.append(ToolCall(id=call_id, name=name, arguments=_parse_arguments(args)))

    finish = getattr(choice, "finish_reason", None) or "stop"
    # Several providers report "stop" on a turn that carries tool calls. The
    # calls are the ground truth, not the label.
    stop_reason = "tool_use" if tool_calls else _STOP_REASONS.get(finish, finish)

    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    for call in tool_calls:
        content.append(
            {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
        )
    return Reply(
        text=text,
        content=content,
        tool_calls=tool_calls,
        stop_reason=stop_reason,
        model=getattr(response, "model", None) or model,
        usage=_usage_of(getattr(response, "usage", None)),
        latency_ms=latency_ms,
        cost_usd=cost_usd,
    )


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


_litellm_configured = False


def _litellm():
    global _litellm_configured
    import litellm

    if not _litellm_configured:
        # Drop per-provider params a backend rejects rather than erroring, and
        # send no usage telemetry from a user's own-key call.
        litellm.drop_params = True
        litellm.telemetry = False
        litellm.suppress_debug_info = True
        _litellm_configured = True
    return litellm


def _supports_reasoning(model: str) -> bool:
    """Whether LiteLLM knows this model to take a reasoning-effort setting.

    An unknown model gets no setting rather than an error.
    """
    try:
        return bool(_litellm().supports_reasoning(model=model))
    except Exception:  # noqa: BLE001 - not in LiteLLM's map
        return False


def _retryable(exc: Exception) -> bool:
    name = type(exc).__name__
    return name in {
        "RateLimitError",
        "ServiceUnavailableError",
        "InternalServerError",
        "APIConnectionError",
        "Timeout",
        "APIError",
    } or "overloaded" in str(exc).lower()


def _hopeless(exc: Exception) -> bool:
    """A rate-limit-shaped error that waiting cannot fix.

    Groq answers a single request larger than the per-minute token limit with a
    429-like "Request too large", and Gemini reports an exhausted *daily*
    quota the same way it reports a per-minute one. Retrying either burns the
    backoff budget and then fails anyway.
    """
    text = str(exc).lower()
    return "request too large" in text or "perday" in text or "per day" in text


def _first_line(exc: Exception) -> str:
    text = str(exc).strip().splitlines()
    return text[0][:300] if text else type(exc).__name__


class LLMClient:
    """Messages in, `Reply` out.

    Retries are this client's own (LiteLLM's are switched off so they do not
    compound): rate limits and transient server errors back off exponentially,
    which on a free tier is the difference between a run and a crash.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = STRONG_MODEL,
        max_retries: int = 6,
        timeout: float = 300.0,
        backoff_cap: float = 60.0,
    ) -> None:
        self.model = model
        self._api_key = api_key
        self._max_retries = max_retries
        self._timeout = timeout
        self._backoff_cap = backoff_cap

    def _keep_cache_control(self, model: str) -> bool:
        return provider_of(model) in EXPLICIT_CACHE_PROVIDERS

    async def validate(self, *models: str) -> None:
        """Fail at startup, not on the first live request.

        Checks that a key for each model's provider is present. LiteLLM has no
        uniform "list the models this key may use" call across providers, so a
        retired model id still surfaces as a NotFound on the first request —
        mapped to an LLMError naming the model.
        """
        litellm = _litellm()
        wanted = {m for m in models if m} or {self.model}
        problems = []
        for model in sorted(wanted):
            if self._api_key:
                continue
            try:
                env = litellm.validate_environment(model=model)
            except Exception as exc:  # noqa: BLE001 - an unroutable string
                problems.append(f"{model!r}: {exc}")
                continue
            if not env.get("keys_in_environment", True):
                missing = ", ".join(env.get("missing_keys") or []) or "its API key"
                problems.append(f"{model!r} needs {missing}")
        if problems:
            raise LLMError(
                "Cannot reach these models:\n  "
                + "\n  ".join(problems)
                + "\nPut the key in .env, or choose another model with --model."
            )

    def _request(
        self,
        messages: list[dict[str, Any]],
        *,
        system: list[dict[str, Any]] | str | None,
        tools: list[dict[str, Any]] | None,
        model: str,
        max_tokens: int,
        temperature: float | None,
        effort: str | None,
    ) -> dict[str, Any]:
        keep = self._keep_cache_control(model)
        wire = to_openai_messages(messages)
        sys_msg = to_openai_system(system, keep_cache_control=keep)
        if sys_msg is not None:
            wire = [sys_msg, *wire]
        params: dict[str, Any] = {
            "model": model,
            "messages": wire,
            "max_tokens": max_tokens,
            "timeout": self._timeout,
            "num_retries": 0,
        }
        if self._api_key:
            params["api_key"] = self._api_key
        if tools:
            params["tools"] = to_openai_tools(tools)
        if temperature is not None:
            params["temperature"] = temperature
        if effort:
            # The cost dial, where the model has one. LiteLLM maps it onto each
            # provider's reasoning control and drop_params removes it for
            # models without one, so "high" on a Llama model is a no-op rather
            # than an error.
            if _supports_reasoning(model):
                params["reasoning_effort"] = effort
        return params

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        system: list[dict[str, Any]] | str | None = None,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 8192,
        effort: str | None = None,
        thinking: bool = False,
        temperature: float | None = None,
    ) -> Reply:
        litellm = _litellm()
        model = model or self.model
        params = self._request(
            messages,
            system=system,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            effort=effort,
        )

        attempt = 0
        while True:
            start = time.monotonic()
            try:
                response = await litellm.acompletion(**params)
                break
            except litellm.NotFoundError as exc:
                raise LLMError(
                    f"Model {model!r} was not found. It may have been retired, or "
                    "need a provider prefix such as 'groq/' or 'gemini/'."
                ) from exc
            except litellm.AuthenticationError as exc:
                raise LLMError(f"The API key for {model!r} was rejected.") from exc
            except litellm.PermissionDeniedError as exc:
                raise LLMError(f"This key may not use {model!r}.") from exc
            except litellm.ContextWindowExceededError as exc:
                raise LLMError(
                    f"The request no longer fits {model!r}'s context window."
                ) from exc
            except Exception as exc:
                if _hopeless(exc):
                    raise LLMError(f"{model!r}: {_first_line(exc)}") from exc
                if attempt >= self._max_retries or not _retryable(exc):
                    raise
                attempt += 1
                await asyncio.sleep(min(self._backoff_cap, 2.0 ** attempt))

        latency_ms = int((time.monotonic() - start) * 1000)
        reply = reply_from_response(response, model=model, latency_ms=latency_ms, cost_usd=None)
        reply.cost_usd = self._cost(response, reply)
        return reply

    @staticmethod
    def _cost(response: Any, reply: Reply) -> float | None:
        try:
            cost = _litellm().completion_cost(completion_response=response)
        except Exception:  # noqa: BLE001 - not in LiteLLM's map
            cost = None
        if cost:
            return float(cost)
        u = reply.usage
        return price_of(
            reply.model, u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens
        )

    async def count_tokens(
        self,
        messages: list[dict[str, Any]],
        *,
        system: list[dict[str, Any]] | str | None = None,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> int:
        """A local token estimate (LiteLLM's tokenizer for the model, where it
        has one). Only used to re-measure right after compaction: the loop
        otherwise tracks the provider's own count from each reply's usage."""
        model = model or self.model
        wire = to_openai_messages(messages)
        sys_msg = to_openai_system(system, keep_cache_control=False)
        if sys_msg is not None:
            wire = [sys_msg, *wire]
        try:
            return int(
                _litellm().token_counter(
                    model=model, messages=wire, tools=to_openai_tools(tools or []) or None
                )
            )
        except Exception:  # noqa: BLE001 - never let accounting end a turn
            return sum(len(json.dumps(m)) for m in wire) // 4
