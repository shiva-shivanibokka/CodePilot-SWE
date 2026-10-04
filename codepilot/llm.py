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


@dataclass(frozen=True)
class Price:
    """USD per token, and where the numbers came from."""

    input: float
    output: float
    cache_read: float
    #: 5-minute-TTL cache write. Only that TTL is ever requested here.
    cache_write: float
    source: str


_ANTHROPIC_LIST = (
    "Anthropic first-party list price (claude-api reference, models table cached "
    "2026-09-25); cache write = 1.25x input for the 5-minute TTL "
    "(same reference, prompt-caching economics)"
)
_GROQ_LIST = "Groq published on-demand price as known when written; RE-VERIFY before a paid run"

#: Explicit prices, keyed by model id without provider prefix or date suffix.
#: Consulted before LiteLLM's map, because they were checked by hand (D26).
#: Rows whose source says RE-VERIFY were not.
PRICING: dict[str, Price] = {
    "claude-opus-5-5": Price(4.00e-6, 20.00e-6, 0.20e-6, 5.00e-6, _ANTHROPIC_LIST),
    "claude-sonnet-5-5": Price(2.00e-6, 10.00e-6, 0.20e-6, 2.50e-6, _ANTHROPIC_LIST),
    "claude-opus-5": Price(5.00e-6, 25.00e-6, 0.50e-6, 6.25e-6,
                           _ANTHROPIC_LIST + "; matches LiteLLM 1.103.2's map"),
    "claude-sonnet-5": Price(2.00e-6, 10.00e-6, 0.20e-6, 2.50e-6,
                             _ANTHROPIC_LIST + "; matches LiteLLM 1.103.2's map"),
    "claude-haiku-4-5": Price(1.00e-6, 5.00e-6, 0.10e-6, 1.25e-6,
                              _ANTHROPIC_LIST + "; matches LiteLLM 1.103.2's map"),
    "llama-3.3-70b-versatile": Price(0.59e-6, 0.79e-6, 0.59e-6, 0.59e-6, _GROQ_LIST),
    "llama-3.1-8b-instant": Price(0.05e-6, 0.08e-6, 0.05e-6, 0.05e-6, _GROQ_LIST),
    "meta-llama/llama-4-scout-17b-16e-instruct": Price(0.11e-6, 0.34e-6, 0.11e-6, 0.11e-6, _GROQ_LIST),
}

#: Providers that run on your own machine. They cost nothing, by definition,
#: rather than by a missing price being read as zero.
LOCAL_PROVIDERS = {"ollama", "ollama_chat"}

#: Providers that honour an explicit `cache_control` breakpoint. Everyone else
#: has it stripped before the request leaves.
EXPLICIT_CACHE_PROVIDERS = {"anthropic"}


#: Minimum cacheable prefix in tokens (claude-api reference, prompt-caching
#: API table). A shorter prefix silently does not cache.
_CACHE_MINIMUMS = [
    (re.compile(r"claude-(opus-5|fable-5|mythos-5|sonnet-5-5)"), 512),
    (re.compile(r"claude-(opus-4-8|sonnet-5|sonnet-4-[56]|opus-4-1|opus-4-2|sonnet-4-2|opus-4(?!-\d)|sonnet-4(?!-\d))"), 1024),
    (re.compile(r"claude-(opus-4-7|3-5-haiku|haiku-3-5)"), 2048),
    (re.compile(r"claude-(opus-4-[56]|haiku-4-5)"), 4096),
]


def cache_minimum(model: str) -> int | None:
    """The smallest prefix the model will cache, or None if not Anthropic."""
    if "claude" not in model:
        return None
    for pattern, minimum in _CACHE_MINIMUMS:
        if pattern.search(model):
            return minimum
    return None


def mark_latest_for_cache(wire: list[dict[str, Any]]) -> None:
    """Put a cache breakpoint on the last block of the latest message (D34).

    A rolling breakpoint: each request caches the conversation so far, and the
    next reads it back. With the system breakpoint that is two of the four
    Anthropic allows. Applied to every request of every arm alike.
    """
    if not wire:
        return
    last = wire[-1]
    content = last.get("content")
    if isinstance(content, str) and content:
        last["content"] = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
    elif isinstance(content, list) and content and isinstance(content[-1], dict):
        content[-1] = {**content[-1], "cache_control": {"type": "ephemeral"}}


def provider_of(model: str) -> str:
    """The LiteLLM provider a model string routes to, e.g. 'groq'."""
    try:
        import litellm

        return litellm.get_llm_provider(model)[1]
    except Exception:  # noqa: BLE001 - unknown strings fall back to the prefix
        return model.split("/", 1)[0] if "/" in model else "unknown"


def is_local(model: str) -> bool:
    return model.split("/", 1)[0] in LOCAL_PROVIDERS


def _price_key(model: str) -> str:
    bare = model.split("/", 1)[1] if model.split("/", 1)[0] in (
        "anthropic", "groq", "gemini", "openai"
    ) else model
    return re.sub(r"-\d{8}$", "", bare)


def _positive(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def price_for(model: str) -> Price | None:
    """The price of a model, or None when it has none.

    A price of 0 or a missing input/output price counts as **no price**: an
    unpriced paid model must never read as free (D26). Explicit `PRICING`
    rows win over LiteLLM's map. A map entry without cache prices is given
    conservative ones (reads at the input price, writes at 1.25x).
    """
    if is_local(model):
        return Price(0.0, 0.0, 0.0, 0.0, "local model: no charge")
    row = PRICING.get(_price_key(model))
    if row is not None:
        return row
    try:
        from importlib.metadata import version

        import litellm

        info = litellm.get_model_info(model)
        src = f"litellm {version('litellm')} cost map"
    except Exception:  # noqa: BLE001 - not in the map
        return None
    p_in = _positive(info.get("input_cost_per_token"))
    p_out = _positive(info.get("output_cost_per_token"))
    if p_in is None or p_out is None:
        return None
    p_read = _positive(info.get("cache_read_input_token_cost")) or p_in
    p_write = _positive(info.get("cache_creation_input_token_cost")) or p_in * 1.25
    return Price(p_in, p_out, p_read, p_write, src)


def cost_of(model: str, usage: Usage) -> float | None:
    """USD for one call's usage, or None when the model has no price."""
    p = price_for(model)
    if p is None:
        return None
    return (
        usage.input_tokens * p.input
        + usage.output_tokens * p.output
        + usage.cache_read_tokens * p.cache_read
        + usage.cache_write_tokens * p.cache_write
    )


def price_of(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float | None:
    """Cost in USD, or None when the model has no price (kept for callers of
    the original API; `cost_of` is the same with a Usage)."""
    return cost_of(model, Usage(input_tokens, output_tokens, cache_read_tokens, cache_write_tokens))


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
    #: Sampling parameters asked for but not sent, because the model rejects
    #: them or LiteLLM would have dropped them silently (D29).
    omitted_params: list[str] = field(default_factory=list)

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


class AbortRun(LLMError):
    """Stop the whole run, not just this attempt: never scored as a result.

    Raised for the global spend cap (D28) and for a request the provider
    rejected as malformed (D29). The benchmark harness re-raises it rather
    than recording an agent failure.
    """


class ProviderRejected(AbortRun):
    """The provider refused the request itself (a 4xx other than 429).

    A malformed or unsupported request is a harness or configuration bug, not
    something the agent did, so it must stop the run rather than be scored.
    """


class ModelMismatch(AbortRun):
    """The provider answered with a different model than the one requested."""


class SpendCapReached(AbortRun):
    """The next call could take total spend past `max_total_usd`."""


def _ledger_home() -> Path:
    """The user-level directory every CodePilot-SWE checkout shares (D41).

    Outside the repository on purpose: running from another checkout or
    another output directory must not start a fresh ledger. There is no
    environment-variable override; tests monkeypatch `LEDGER_DIR`.
    """
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "sop_eval" / "codepilot_swe"


LEDGER_DIR = _ledger_home()

_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    call_id TEXT PRIMARY KEY,
    at REAL, tag TEXT, model TEXT, reported_model TEXT,
    status TEXT,             -- pending | settled | cached
    cost_usd REAL,           -- what counts: worst case while pending, real once settled
    worst_usd REAL,
    input_tokens INTEGER, output_tokens INTEGER,
    cache_read_tokens INTEGER, cache_write_tokens INTEGER,
    latency_ms INTEGER, stop_reason TEXT, omitted_params TEXT,
    error TEXT
)
"""


class Ledger:
    """Every model request, in one SQLite file (D27, D38, D41).

    * A request is **reserved** before it is sent: in one `BEGIN IMMEDIATE`
      transaction the ledger's total is read, the cap is checked against it
      plus the request's worst case, and a `pending` row charged at that worst
      case is inserted. Check and reserve cannot be separated by another
      writer, so concurrent clients cannot overshoot the cap together.
    * It is **settled** (`UPDATE` to the real cost) only when a response is
      parsed, or at $0 when the provider cleanly rejected it (a 4xx). Any
      other end — a timeout or protocol error mid-body, an error event inside
      a status-200 stream, an interrupt, a kill — leaves it charged at its
      worst case.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(_LEDGER_SCHEMA)

    @classmethod
    def default(cls) -> Ledger:
        return cls(LEDGER_DIR / "ledger.sqlite")

    def _connect(self):
        import sqlite3

        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        return _Closing(db)

    @staticmethod
    def _total(db, tag_prefix: str = "") -> float:
        row = db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM calls WHERE tag LIKE ? ESCAPE '\\'",
            (tag_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",),
        ).fetchone()
        return float(row[0])

    def reserve(self, call_id: str, *, tag: str, model: str, worst: float | None,
                cap: float | None) -> None:
        """Atomically check the cap and insert a pending row at `worst`."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if cap is not None:
                    if worst is None:
                        raise SpendCapReached(f"{model} has no price, so a spend cap cannot be enforced")
                    spent = self._total(db)
                    if spent + worst > cap:
                        raise SpendCapReached(
                            f"spend cap: ${spent:.4f} spent + up to ${worst:.4f} for the next "
                            f"call > ${cap:.2f}"
                        )
                db.execute(
                    "INSERT INTO calls (call_id, at, tag, model, status, cost_usd, worst_usd) "
                    "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                    (call_id, time.time(), tag, model, worst or 0.0, worst),
                )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def settle(self, call_id: str, cost: float | None, **fields: Any) -> None:
        cols = {k: (json.dumps(v) if isinstance(v, list | dict) else v) for k, v in fields.items()}
        cols["status"], cols["cost_usd"] = "settled", cost or 0.0
        sets = ", ".join(f"{k} = ?" for k in cols)
        with self._connect() as db:
            db.execute(f"UPDATE calls SET {sets} WHERE call_id = ?", (*cols.values(), call_id))  # noqa: S608

    def note_error(self, call_id: str, error: str) -> None:
        """Record why a request ended, leaving its worst-case charge in place."""
        with self._connect() as db:
            db.execute("UPDATE calls SET error = ? WHERE call_id = ?", (error, call_id))

    def append(self, row: dict[str, Any]) -> None:
        """Insert a finished row: a cached reply ($0), or spend recorded elsewhere."""
        fields = {k: row.get(k) for k in ("tag", "model", "reported_model", "cost_usd",
                                          "input_tokens", "output_tokens", "cache_read_tokens",
                                          "cache_write_tokens", "stop_reason", "error")}
        with self._connect() as db:
            db.execute(
                "INSERT INTO calls (call_id, at, status, tag, model, reported_model, cost_usd, "
                "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, stop_reason, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row.get("call_id") or uuid.uuid4().hex, row.get("at") or time.time(),
                 row.get("status", "settled"), fields["tag"] or "", fields["model"],
                 fields["reported_model"], float(fields["cost_usd"] or 0.0), fields["input_tokens"],
                 fields["output_tokens"], fields["cache_read_tokens"], fields["cache_write_tokens"],
                 fields["stop_reason"], fields["error"]),
            )

    def rows(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM calls ORDER BY at, rowid")]

    def total_usd(self, tag_prefix: str = "") -> float:
        """Settled costs plus the worst case of every request never settled."""
        with self._connect() as db:
            return self._total(db, tag_prefix)


class _Closing:
    def __init__(self, db) -> None:
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *exc) -> None:
        self.db.close()


@dataclass
class TagSpend:
    """What one tag (an instance's arm, say) has spent through this client."""

    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    calls: int = 0
    unpriced_calls: int = 0
    #: Worst-case cost of requests that never settled (D38).
    unsettled_usd: float = 0.0


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
    become `tool_calls`. Assistant `thinking` / `redacted_thinking` blocks
    travel as `thinking_blocks`, unchanged, which is where LiteLLM's Anthropic
    route puts them back in front of the tool calls (D31). They are only valid
    on the model that produced them; `_request` strips them for other
    providers.
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
            thinking = [b for b in blocks if b.get("type") in ("thinking", "redacted_thinking")]
            if thinking:
                msg["thinking_blocks"] = thinking
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
    # Thinking first, exactly as returned (signature included): Claude 5-family
    # models think by default and require these blocks back, unchanged, ahead
    # of the tool calls they preceded (D31).
    for block in getattr(message, "thinking_blocks", None) or []:
        block = _block(block)
        if block.get("type") in ("thinking", "redacted_thinking"):
            content.append(dict(block))
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


#: Models that reject `temperature`/`top_p`/`top_k` with a 400 (claude-api
#: reference, thinking & effort table): Opus 4.7/4.8, Opus 5/5.5, Sonnet 5 and
#: 5.5 (non-default values), Fable/Mythos 5.x. Haiku 4.5 accepts them.
_REJECTS_SAMPLING = re.compile(r"claude-(opus-4-[78]|opus-5|sonnet-5|fable-5|mythos-5)")


def _sends(model: str, param: str) -> bool:
    """Whether `param` would actually reach the provider for `model`.

    False for a model known to reject it, and for any parameter LiteLLM does
    not list as supported for the model — those it drops without a word
    (`drop_params=True`), which is how a "seeded" Claude run used to be
    recorded as seeded.
    """
    if param in ("temperature", "top_p") and _REJECTS_SAMPLING.search(model):
        return False
    try:
        provider = provider_of(model)
        bare = model.split("/", 1)[1] if model.startswith(provider + "/") else model
        supported = _litellm().get_supported_openai_params(model=bare, custom_llm_provider=provider)
    except Exception:  # noqa: BLE001 - unknown: do not send
        return False
    return bool(supported) and param in supported


def _status_of(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def estimate_prompt_tokens(messages: list[dict[str, Any]], tools: Any = None) -> int:
    """A deliberately generous token estimate for a request (D38).

    max(characters / 2.5, UTF-8 bytes / 3): the byte term keeps non-Latin
    text, which tokenizers split far more finely than English, from being
    undercounted the way a characters-per-token rule would.
    """
    text = json.dumps({"m": messages, "t": tools}, ensure_ascii=False, default=str)
    return int(max(len(text) / 2.5, len(text.encode("utf-8")) / 3))


def _status_200_error(exc: BaseException) -> bool:
    """An error event delivered inside a successful (200) response."""
    text = str(exc).lower()
    return "overloaded_error" in text or '"api_error"' in text or "'api_error'" in text


def _transport_error(exc: BaseException) -> bool:
    """A transport failure from either HTTP stack that can reach us (D41).

    LiteLLM 1.103.2 uses `httpx`: it maps `httpx.TimeoutException` to
    `litellm.Timeout`, retries `RemoteProtocolError`/`ConnectError` once
    itself on a new connection, and lets a second one through raw. `httpx2`
    (what the Anthropic SDK 1.x uses) is installed alongside and is checked
    too, in case a route surfaces its errors.
    """
    types: list[type] = []
    for module in ("httpx", "httpx2"):
        try:
            types.append(__import__(module).TransportError)
        except (ImportError, AttributeError):
            continue
    return isinstance(exc, tuple(types)) if types else False


def _bare_model(model: str) -> str:
    """A model id without provider prefix or date suffix, for comparison."""
    head, _, rest = model.partition("/")
    if rest and head in ("anthropic", "groq", "gemini", "openai", "ollama", "ollama_chat"):
        model = rest
    return re.sub(r"-\d{8}$", "", model)


def _clean_rejection(exc: BaseException) -> bool:
    """A 4xx the provider answered before doing any work: not billed, not
    retried, and (D29) the end of the run. 408 (request timeout) and 429
    (rate limit) are excluded: both are transient, and a 408 can arrive after
    the model started work."""
    status = _status_of(exc)
    return (status is not None and 400 <= status < 500 and status not in (408, 429)
            and not _status_200_error(exc) and not _transport_error(exc))


def _retryable(exc: Exception) -> bool:
    if _transport_error(exc):
        return True
    name = type(exc).__name__
    return name in {
        "RateLimitError",
        "ServiceUnavailableError",
        "InternalServerError",
        "APIConnectionError",
        "Timeout",
        "APIError",
    } or "overloaded" in str(exc).lower() or _status_200_error(exc)


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

    Retries are this client's own and nobody else's (LiteLLM's and the
    OpenAI SDK's are switched off so they cannot compound): one retry after a
    rate limit or transient server error, so a call is at most two requests
    (D30).
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = STRONG_MODEL,
        max_retries: int = 1,
        timeout: float = 300.0,
        backoff_cap: float = 60.0,
        api_base: str | None = None,
        extra: dict[str, Any] | None = None,
        ledger: Ledger | None = None,
        max_total_usd: float | None = None,
        response_cache: Path | str | None = None,
        max_prompt_tokens: int | None = None,
    ) -> None:
        self.model = model
        #: Refuse (as an arm failure, not a run abort) any request whose
        #: generous estimate exceeds this, so every call has a known worst case
        #: and a design's worst case can be computed before it runs (D39).
        self.max_prompt_tokens = max_prompt_tokens
        #: Directory of stored replies keyed by (model, request hash, tag), so
        #: re-running a benchmark does not pay twice for identical requests.
        self.response_cache = Path(response_cache) if response_cache else None
        #: Hard ceiling on everything this client spends, both benchmark arms
        #: and compaction included, plus whatever the ledger already holds
        #: from earlier runs. Checked before every request (D28).
        self.max_total_usd = max_total_usd
        #: Every request is reserved, then settled, here (D27, D38, D41). There
        #: is no unledgered client: without an explicit ledger, the user-level
        #: one every checkout shares is used.
        self.ledger = ledger if ledger is not None else Ledger.default()
        #: Label written with each call and used to total spend per arm. The
        #: benchmark sets it to "<instance>:<arm>" before each arm.
        self.tag = ""
        self._spend: dict[str, TagSpend] = {}
        self._unsettled: dict[str, tuple[str, float]] = {}
        self._api_key = api_key
        #: For a self-hosted server, e.g. an Ollama endpoint.
        self._api_base = api_base
        #: Provider-specific request options, sent as-is (Ollama's num_ctx).
        self._extra = dict(extra or {})
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
        seed: int | None = None,
    ) -> dict[str, Any]:
        keep = self._keep_cache_control(model)
        wire = to_openai_messages(messages)
        if not keep:
            # Thinking blocks are bound to the Anthropic model that wrote them.
            for m in wire:
                m.pop("thinking_blocks", None)
        else:
            mark_latest_for_cache(wire)
        sys_msg = to_openai_system(system, keep_cache_control=keep)
        if sys_msg is not None:
            wire = [sys_msg, *wire]
        if model.startswith("ollama/"):
            # LiteLLM's `ollama/` route is /api/generate, which emulates tool
            # calls by forcing JSON output and accepting only a top-level
            # {"name", "arguments"} object; a model that answers in the nested
            # OpenAI shape gets its call passed through as text (D24).
            # `ollama_chat/` is Ollama's native /api/chat, which parses the
            # model's own tool-call format. Same server, same model.
            model = "ollama_chat/" + model.removeprefix("ollama/")
        params: dict[str, Any] = {
            "model": model,
            "messages": wire,
            "max_tokens": max_tokens,
            "timeout": self._timeout,
            # One retry layer only, ours (D30): LiteLLM's router retries and
            # the OpenAI SDK's own default of 2 (used by openai-compatible
            # routes such as Groq) are both switched off.
            "num_retries": 0,
            "max_retries": 0,
        }
        if self._api_key:
            params["api_key"] = self._api_key
        if tools:
            params["tools"] = to_openai_tools(tools)
        omitted: list[str] = []
        if temperature is not None:
            if _sends(model, "temperature"):
                params["temperature"] = temperature
            else:
                omitted.append("temperature")
        if seed is not None:
            if _sends(model, "seed"):
                params["seed"] = seed
            else:
                omitted.append("seed")
        self._omitted = omitted
        if self._api_base:
            params["api_base"] = self._api_base
        params.update(self._extra)
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
        seed: int | None = None,
        cache_tag: str = "",
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
            seed=seed,
        )

        self._check_fits(params, max_tokens)
        if self.max_prompt_tokens is not None:
            estimate = estimate_prompt_tokens(params["messages"], params.get("tools"))
            if estimate > self.max_prompt_tokens:
                raise LLMError(
                    f"context overflow: prompt bound exceeded (~{estimate} tokens estimated > "
                    f"{self.max_prompt_tokens}); not sent"
                )

        key = self._cache_key(params, cache_tag)
        cached = self._cache_get(key, model)
        if cached is not None:
            return cached

        attempt = 0
        while True:
            call_id = uuid.uuid4().hex
            # Cap check and reservation in one transaction (D41).
            self._open(call_id, model, self.worst_case_usd(model, params, max_tokens))
            start = time.monotonic()
            try:
                response = await litellm.acompletion(**params)
                break
            except BaseException as exc:
                self._record(model, error=exc, call_id=call_id)
                if not isinstance(exc, Exception):
                    raise
                failure = exc
            try:
                raise failure
            except litellm.ContextWindowExceededError as exc:
                raise ProviderRejected(
                    f"{model!r} rejected the request as too long for its context window "
                    "(a 4xx: the run stops; see D29)"
                ) from exc
            except litellm.NotFoundError as exc:
                raise ProviderRejected(
                    f"Model {model!r} was not found. It may have been retired, or "
                    "need a provider prefix such as 'groq/' or 'gemini/'."
                ) from exc
            except litellm.AuthenticationError as exc:
                raise ProviderRejected(f"The API key for {model!r} was rejected.") from exc
            except litellm.PermissionDeniedError as exc:
                raise ProviderRejected(f"This key may not use {model!r}.") from exc
            except Exception as exc:
                status = _status_of(exc)
                if _clean_rejection(exc):
                    raise ProviderRejected(
                        f"{model!r} rejected the request ({status}): {_first_line(exc)}"
                    ) from exc
                if _hopeless(exc):
                    raise LLMError(f"{model!r}: {_first_line(exc)}") from exc
                if attempt >= self._max_retries or not _retryable(exc):
                    raise
                attempt += 1
                await asyncio.sleep(min(self._backoff_cap, 2.0 ** attempt))

        latency_ms = int((time.monotonic() - start) * 1000)
        reply = reply_from_response(response, model=model, latency_ms=latency_ms, cost_usd=None)
        # Priced from the model that was asked for, by our own table first
        # (D26); LiteLLM's completion_cost is not used, so 0 can never stand in
        # for "unknown".
        reply.cost_usd = cost_of(model, reply.usage)
        reply.omitted_params = list(getattr(self, "_omitted", []))
        self._record(model, reply=reply, call_id=call_id)
        if reply.model and _bare_model(reply.model) != _bare_model(model):
            # Recorded (and settled) above under the model that answered; the
            # run cannot continue on a model it did not ask for (D41).
            raise ModelMismatch(f"asked for {model!r}, the provider answered as {reply.model!r}")
        self._cache_put(key, reply)
        return reply

    # ------------------------------------------------------- response cache

    @staticmethod
    def _cache_key(params: dict[str, Any], cache_tag: str) -> str:
        """Hash of everything that decides the reply, plus the caller's tag.

        The tag is what keeps attempt 2 from replaying attempt 1 when the
        provider takes no seed and the requests are otherwise identical.
        """
        import hashlib

        material = {k: v for k, v in params.items() if k not in ("api_key", "timeout")}
        material["_tag"] = cache_tag
        return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()

    def _cache_path(self, key: str) -> Path | None:
        return None if self.response_cache is None else self.response_cache / f"{key}.json"

    def _cache_get(self, key: str, model: str) -> Reply | None:
        path = self._cache_path(key)
        if path is None or not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        reply = Reply(
            text=data["text"],
            content=data["content"],
            tool_calls=[ToolCall(**c) for c in data["tool_calls"]],
            stop_reason=data["stop_reason"],
            model=data["model"],
            usage=Usage(**data["usage"]),
            latency_ms=0,
            cost_usd=0.0,
            omitted_params=data.get("omitted_params", []),
        )
        # Not billed: recorded at $0, flagged, with the original usage kept so
        # token measurements still mean something.
        self._record(model, reply=reply, cached=True)
        return reply

    def _cache_put(self, key: str, reply: Reply) -> None:
        path = self._cache_path(key)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "text": reply.text, "content": reply.content,
            "tool_calls": [vars(c) for c in reply.tool_calls],
            "stop_reason": reply.stop_reason, "model": reply.model,
            "usage": vars(reply.usage), "omitted_params": reply.omitted_params,
            "original_cost_usd": reply.cost_usd,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, default=str), encoding="utf-8")
        tmp.replace(path)

    def _open(self, call_id: str, model: str, worst: float | None) -> None:
        """Reserve a request at its worst case before it is sent (D38, D41).

        Raises SpendCapReached, inside the same transaction that would have
        reserved it, if the cap does not allow it.
        """
        self.ledger.reserve(call_id, tag=self.tag, model=model, worst=worst, cap=self.max_total_usd)
        spend = self._spend.setdefault(self.tag, TagSpend())
        self._unsettled[call_id] = (self.tag, worst or 0.0)
        spend.unsettled_usd += worst or 0.0

    def _settle(self, call_id: str | None) -> None:
        if call_id is None or call_id not in self._unsettled:
            return
        tag, worst = self._unsettled.pop(call_id)
        self._spend.setdefault(tag, TagSpend()).unsettled_usd -= worst

    def _record(self, model: str, *, reply: Reply | None = None, error: BaseException | None = None,
                cached: bool = False, call_id: str | None = None) -> None:
        """Account for one request's outcome, in memory and in the ledger."""
        spend = self._spend.setdefault(self.tag, TagSpend())
        if reply is not None:
            spend.usage = spend.usage + reply.usage
            spend.calls += 1
            if reply.cost_usd is None:
                spend.unpriced_calls += 1
            else:
                spend.cost_usd += reply.cost_usd
            fields = dict(
                reported_model=reply.model,
                input_tokens=reply.usage.input_tokens,
                output_tokens=reply.usage.output_tokens,
                cache_read_tokens=reply.usage.cache_read_tokens,
                cache_write_tokens=reply.usage.cache_write_tokens,
                stop_reason=reply.stop_reason,
            )
            if cached:
                self.ledger.append({"status": "cached", "tag": self.tag, "model": model,
                                    "cost_usd": 0.0, **fields})
            elif call_id is not None:
                self.ledger.settle(call_id, reply.cost_usd, latency_ms=reply.latency_ms,
                                   omitted_params=reply.omitted_params, **fields)
                self._settle(call_id)
        if error is not None and call_id is not None:
            message = f"{type(error).__name__}: {_first_line(error)}"
            if _clean_rejection(error):
                # The provider refused the request before doing any work: not
                # billed. Settled at $0.
                self.ledger.settle(call_id, 0.0, error=message)
                self._settle(call_id)
            else:
                # Ended mid-flight, or in a way that may still be billed: the
                # pending row keeps its worst-case charge (D38).
                self.ledger.note_error(call_id, message)

    #: Applied to LiteLLM's prompt-token estimate in the spend cap's worst
    #: case. LiteLLM counts with a generic tokenizer; for qwen2.5:7b it
    #: undercounted by 1.39x (D22). 1.5x is a judgement for providers whose
    #: tokenizer it does not know, Anthropic's included.
    HOSTED_COUNT_MARGIN = 1.5

    def total_spent_usd(self) -> float:
        """Everything the ledger holds: every checkout's and every run's (D41)."""
        return self.ledger.total_usd()

    def worst_case_usd(self, model: str, params: dict[str, Any], max_tokens: int) -> float | None:
        """The most the next request could cost, or None if it is unpriced.

        Every prompt token priced as a cache *write* (the dearest input
        class) and the full `max_tokens` of output.
        """
        price = price_for(model)
        if price is None:
            return None
        try:
            estimate = _litellm().token_counter(
                model=params["model"], messages=params["messages"], tools=params.get("tools")
            )
        except Exception:  # noqa: BLE001 - fall back to characters
            estimate = sum(len(json.dumps(m)) for m in params["messages"]) // 3
        prompt = max(int(estimate * self.HOSTED_COUNT_MARGIN),
                     estimate_prompt_tokens(params["messages"], params.get("tools")))
        return prompt * max(price.input, price.cache_write) + max_tokens * price.output

    def spent(self, tag_prefix: str = "") -> TagSpend:
        """Total spend of every tag starting with `tag_prefix`."""
        total = TagSpend()
        for tag, s in self._spend.items():
            if tag.startswith(tag_prefix):
                total.usage = total.usage + s.usage
                total.cost_usd += s.cost_usd
                total.calls += s.calls
                total.unpriced_calls += s.unpriced_calls
                total.unsettled_usd += s.unsettled_usd
        return total

    #: LiteLLM's token count for a local model is a generic tokenizer and
    #: undercounts what the model's chat template produces: measured 1,396
    #: against Ollama's own 1,938 for the benchmark agent's first request
    #: (tool schemas included). 1.4x covers that with little to spare.
    LOCAL_COUNT_MARGIN = 1.4

    def _check_fits(self, params: dict[str, Any], max_tokens: int) -> None:
        """Refuse a request that cannot fit an explicit context window.

        Only when `num_ctx` is set (a local Ollama model). Ollama does not
        reject an oversized prompt: it truncates it silently, from the front,
        which drops the system prompt — reproduced on qwen2.5:7b, where a
        ~26k-token prompt at num_ctx=16384 came back as prompt_tokens=8194
        with the instructions gone. Failing loudly turns that into a recorded
        error instead of a mysteriously bad answer.
        """
        num_ctx = self._extra.get("num_ctx")
        if not num_ctx:
            return
        try:
            estimate = _litellm().token_counter(
                model=params["model"], messages=params["messages"], tools=params.get("tools")
            )
        except Exception:  # noqa: BLE001 - fall back to characters
            estimate = sum(len(json.dumps(m)) for m in params["messages"]) // 4
        needed = int(estimate * self.LOCAL_COUNT_MARGIN) + max_tokens
        if needed > int(num_ctx):
            raise LLMError(
                f"context overflow: ~{int(estimate * self.LOCAL_COUNT_MARGIN)} prompt tokens "
                f"+ {max_tokens} output > num_ctx {num_ctx}"
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
