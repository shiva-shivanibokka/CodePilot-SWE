"""
Additional tests for context window management edge cases.
"""

from __future__ import annotations

from agent.context import (
    CONTEXT_BUDGET,
    compress_messages,
    count_messages_tokens,
    count_tokens,
    get_budget_status,
)


class TestTokenCounting:
    def test_empty_string(self):
        assert count_tokens("") == 0

    def test_longer_text_has_more_tokens(self):
        short = count_tokens("Hi")
        long = count_tokens("Hi " * 100)
        assert long > short

    def test_messages_tokens_sum(self):
        messages = [
            {"role": "user", "content": "Hello world"},
            {"role": "assistant", "content": "I will help"},
        ]
        total = count_messages_tokens(messages)
        assert total > 0

    def test_tool_calls_counted(self):
        # OpenAI/LiteLLM format: assistant carries tool_calls, results are role "tool".
        messages = [
            {
                "role": "assistant",
                "content": "Running a command",
                "tool_calls": [
                    {
                        "id": "t1",
                        "type": "function",
                        "function": {"name": "bash", "arguments": '{"command": "ls"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "t1", "content": "file_a.py\nfile_b.py"},
        ]
        total = count_messages_tokens(messages)
        assert total > 0


class TestCompression:
    def _make_big_history(self, n_turns=30):
        # OpenAI/LiteLLM format: assistant(content + tool_calls) then role "tool" result.
        msgs = []
        for i in range(n_turns):
            msgs.append(
                {
                    "role": "assistant",
                    "content": f"I am thinking about step {i}. " * 20,
                    "tool_calls": [
                        {
                            "id": f"t{i}",
                            "type": "function",
                            "function": {
                                "name": "bash",
                                "arguments": f'{{"command": "pytest # turn {i}"}}',
                            },
                        }
                    ],
                }
            )
            msgs.append(
                {"role": "tool", "tool_call_id": f"t{i}", "content": f"5 passed, 0 failed # turn {i}"}
            )
        return msgs

    def test_compress_produces_valid_message_list(self):
        msgs = self._make_big_history(30)
        compressed = compress_messages(msgs)
        # Every message must have role and content
        for msg in compressed:
            assert "role" in msg
            assert "content" in msg

    def test_compress_keeps_recent_messages(self):
        msgs = self._make_big_history(30)
        last_msg = msgs[-1]
        compressed = compress_messages(msgs)
        assert last_msg in compressed

    def test_compress_idempotent_on_small_list(self):
        msgs = [{"role": "user", "content": "small"}]
        result = compress_messages(msgs)
        assert result == msgs

    def test_compress_tail_not_orphan_tool(self):
        # A "tool" message whose assistant tool_calls got compressed away would be
        # rejected by OpenAI/Groq. The tail must start on an assistant turn.
        msgs = self._make_big_history(30)
        compressed = compress_messages(msgs)
        assert compressed[0]["role"] == "user"  # the injected summary
        assert compressed[1]["role"] != "tool"

    def test_budget_status_structure(self):
        msgs = [{"role": "user", "content": "test"}]
        status = get_budget_status(msgs)
        assert status["budget_tokens"] == CONTEXT_BUDGET
        assert isinstance(status["needs_compression"], bool)
        assert 0 <= status["percent_used"] <= 100
