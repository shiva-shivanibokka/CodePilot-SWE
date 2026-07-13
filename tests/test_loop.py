"""
Unit tests for the agent loop's control flow.

The model call (`complete`) and tool dispatch are mocked, so these run without
litellm, Docker, or any network — they exercise the loop itself: turn counting,
<DONE> detection, tool-call round-tripping, and error handling.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from agent import loop
from agent.llm import LLMConfig, LLMError, LLMResponse, ToolCall

LLM = LLMConfig(provider="anthropic", model="claude-sonnet-5", api_key="sk-test")


def _mock_ws():
    ws = MagicMock()
    ws.task_id = "test-loop"
    ws.get_diff.return_value = "--- a/x.py\n+++ b/x.py\n+fixed\n"
    return ws


def _drive(gen):
    """Run the generator to completion, returning (events, TaskResult)."""
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as e:
        return events, e.value


def _resp_tool():
    return LLMResponse(
        text="Let me look around.",
        tool_calls=[ToolCall(id="t1", name="bash", arguments={"command": "ls"})],
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.001,
        finish_reason="tool_calls",
    )


def _resp_done():
    return LLMResponse(
        text="Fixed it. <DONE>changed foo</DONE>",
        tool_calls=[],
        input_tokens=8,
        output_tokens=4,
        cost_usd=0.001,
        finish_reason="stop",
    )


def test_tool_then_done(monkeypatch):
    responses = [_resp_tool(), _resp_done()]
    monkeypatch.setattr(loop, "complete", lambda *a, **k: responses.pop(0))
    monkeypatch.setattr(loop, "_dispatch_tool", lambda *a, **k: "ls output")

    events, result = _drive(loop.run_agent(_mock_ws(), "fix the bug", LLM))

    assert result.stop_reason == "done"
    assert result.conclusion == "changed foo"
    assert result.turns == 2
    assert result.cost_usd > 0
    assert result.error is None

    types = [e.type.value for e in events]
    assert "tool_call" in types
    assert "tool_result" in types
    assert types[-1] == "done"


def test_immediate_done_no_tools(monkeypatch):
    monkeypatch.setattr(loop, "complete", lambda *a, **k: _resp_done())

    _events, result = _drive(loop.run_agent(_mock_ws(), "trivial", LLM))

    assert result.stop_reason == "done"
    assert result.turns == 1


def test_llm_error_is_surfaced(monkeypatch):
    def boom(*a, **k):
        raise LLMError("invalid api key")

    monkeypatch.setattr(loop, "complete", boom)

    events, result = _drive(loop.run_agent(_mock_ws(), "anything", LLM))

    assert result.stop_reason == "error"
    assert result.error == "invalid api key"
    assert any(e.type.value == "error" for e in events)


def test_max_turns_respected(monkeypatch):
    # Model never signals done — loop must stop at max_turns.
    monkeypatch.setattr(loop, "complete", lambda *a, **k: _resp_tool())
    monkeypatch.setattr(loop, "_dispatch_tool", lambda *a, **k: "still going")

    _events, result = _drive(loop.run_agent(_mock_ws(), "loops forever", LLM, max_turns=3))

    assert result.turns == 3
    assert result.stop_reason != "done"
