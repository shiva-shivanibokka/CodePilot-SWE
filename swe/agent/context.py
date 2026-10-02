"""
Context window budget manager.

The most common agent failure mode is hitting the model's context limit mid-task
and either crashing or producing garbage output. This module actively tracks token
usage and compresses the message history when the budget is close to exhaustion.

Strategy:
1. Count tokens in the current message list using tiktoken (cl100k_base).
2. When usage exceeds COMPRESS_THRESHOLD (default 80% of budget), compress old
   tool call/result pairs into a summary message.
3. The compressor summarises each tool interaction in one line, preserving the
   model's reasoning text verbatim (that's what matters for the next turn).
4. The system prompt and the last N messages are always kept verbatim.

Reference: Anthropic's model has a 200k context window. We default to a budget
of 150k to leave headroom for the model's response.
"""

from __future__ import annotations

import os
from typing import Any

import tiktoken

CONTEXT_BUDGET = int(os.getenv("AGENT_CONTEXT_BUDGET", "150000"))
COMPRESS_THRESHOLD = 0.80  # compress when at 80% of budget
MIN_MESSAGES_TO_KEEP = 6  # always keep the last N messages verbatim

# tiktoken encoding (cl100k_base is used by Claude/GPT-4 family)
try:
    _enc = tiktoken.get_encoding("cl100k_base")
except Exception:
    _enc = None


def count_tokens(text: str) -> int:
    """Count tokens in a string. Falls back to len/4 if tiktoken unavailable."""
    if _enc is None:
        return len(text) // 4
    return len(_enc.encode(text, disallowed_special=()))


def count_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """Count total tokens across an OpenAI/LiteLLM-format message list."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += count_tokens(content)
        elif isinstance(content, list):
            # defensive: some providers accept content-part lists
            for block in content:
                if isinstance(block, dict):
                    total += count_tokens(str(block.get("text", "")))
                    total += count_tokens(str(block.get("content", "")))
        # assistant tool calls carry the function name + JSON arguments
        for tc in msg.get("tool_calls", []) or []:
            fn = tc.get("function", {})
            total += count_tokens(str(fn.get("name", "")))
            total += count_tokens(str(fn.get("arguments", "")))
    return total


def should_compress(messages: list[dict[str, Any]]) -> bool:
    """Return True if the message history is approaching the context budget."""
    used = count_messages_tokens(messages)
    return used >= int(CONTEXT_BUDGET * COMPRESS_THRESHOLD)


def compress_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Compress old tool interactions into a summary to free up context space.

    Keeps:
    - Assistant reasoning text from old turns (as a summary)
    - The last ~MIN_MESSAGES_TO_KEEP messages verbatim
    - A single summary user message standing in for old tool calls

    The tail is snapped to an assistant-message boundary so it never starts with
    an orphan "tool" message — OpenAI/Groq reject a tool result whose matching
    assistant tool_calls have been compressed away.
    """
    if len(messages) <= MIN_MESSAGES_TO_KEEP + 2:
        return messages

    # Find a clean cut: the earliest assistant message at/after the nominal tail
    # start. Landing on an assistant turn keeps each tool_calls/tool pair intact.
    start = len(messages) - MIN_MESSAGES_TO_KEEP
    while start < len(messages) and messages[start].get("role") != "assistant":
        start += 1
    if start >= len(messages):
        start = len(messages) - MIN_MESSAGES_TO_KEEP

    keep_tail = messages[start:]
    compress_head = messages[:start]

    summary_lines = [
        "=== COMPRESSED HISTORY (earlier tool interactions) ===",
        "The following happened earlier in this session:\n",
    ]

    for msg in compress_head:
        role = msg.get("role", "")
        content = msg.get("content", "") or ""

        if role == "assistant":
            if isinstance(content, str) and content.strip():
                summary_lines.append(f"[THOUGHT] {content[:500]}")
            for tc in msg.get("tool_calls", []) or []:
                fn = tc.get("function", {})
                name = fn.get("name", "?")
                args = fn.get("arguments", "")
                summary_lines.append(f"[TOOL CALL] {name}: {str(args)[:200]}")

        elif role == "tool":
            if isinstance(content, str) and content.strip():
                summary_lines.append(f"  → RESULT: {content[:300]}")

        elif role == "user" and isinstance(content, str) and content.strip():
            summary_lines.append(f"[USER] {content[:500]}")

    summary_msg = {"role": "user", "content": "\n".join(summary_lines)}
    return [summary_msg, *keep_tail]


def get_budget_status(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a dict with current token usage stats."""
    used = count_messages_tokens(messages)
    return {
        "used_tokens": used,
        "budget_tokens": CONTEXT_BUDGET,
        "percent_used": round(used / CONTEXT_BUDGET * 100, 1),
        "needs_compression": should_compress(messages),
    }
