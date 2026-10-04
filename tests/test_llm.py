"""The LiteLLM seam: translation both ways, caching, usage, cost and errors.

No network. `litellm.acompletion` is replaced with a stub that records what it
was sent and replies with a hand-built response.
"""

from __future__ import annotations

from types import SimpleNamespace

import litellm
import pytest

from codepilot import llm
from codepilot.context import Conversation
from codepilot.llm import (
    LLMClient,
    LLMError,
    reply_from_response,
    to_openai_messages,
    to_openai_system,
)


def response(text="", tool_calls=(), finish="stop", prompt=100, completion=20, **usage_extra):
    calls = [
        SimpleNamespace(
            id=cid, function=SimpleNamespace(name=name, arguments=args)
        )
        for cid, name, args in tool_calls
    ]
    return SimpleNamespace(
        model="stub-model",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=text, tool_calls=calls or None),
                finish_reason=finish,
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=prompt, completion_tokens=completion, **usage_extra
        ),
    )


# ----------------------------------------------------------- translation out


def test_tool_use_and_tool_result_blocks_become_openai_tool_messages():
    convo = Conversation(system_prompt="SYS")
    convo.user("fix it")
    convo.assistant(
        [
            {"type": "text", "text": "looking"},
            {"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {"path": "a.py"}},
            {"type": "tool_use", "id": "tu_2", "name": "list_files", "input": {}},
        ]
    )
    convo.tool_results(
        [
            Conversation.tool_result("tu_1", "contents"),
            Conversation.tool_result("tu_2", "boom", is_error=True),
        ]
    )
    wire = to_openai_messages(convo.messages)
    assert wire[0] == {"role": "user", "content": "fix it"}
    assert wire[1]["role"] == "assistant"
    assert wire[1]["content"] == "looking"
    assert [c["id"] for c in wire[1]["tool_calls"]] == ["tu_1", "tu_2"]
    assert wire[1]["tool_calls"][0]["function"]["arguments"] == '{"path": "a.py"}'
    # One tool message per result, in order, ids preserved.
    assert wire[2] == {"role": "tool", "tool_call_id": "tu_1", "content": "contents"}
    assert wire[3]["tool_call_id"] == "tu_2"
    assert wire[3]["content"].startswith("ERROR: "), "an error result must read as one"


def test_an_assistant_turn_with_only_tool_calls_has_null_content():
    wire = to_openai_messages(
        [{"role": "assistant", "content": [{"type": "tool_use", "id": "x", "name": "f", "input": {}}]}]
    )
    assert wire[0]["content"] is None
    assert wire[0]["tool_calls"][0]["function"]["name"] == "f"


def test_cache_breakpoint_survives_only_where_the_provider_honours_it():
    blocks = Conversation(system_prompt="SYS", project_instructions="PROJ").system_blocks()
    kept = to_openai_system(blocks, keep_cache_control=True)
    assert kept["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in kept["content"][0]
    stripped = to_openai_system(blocks, keep_cache_control=False)
    assert isinstance(stripped["content"], str)
    assert "cache_control" not in stripped["content"]
    assert "SYS" in stripped["content"] and "PROJ" in stripped["content"]


# ------------------------------------------------------------ translation in


def test_tool_calls_win_over_a_stop_label():
    """Several providers say finish_reason='stop' on a turn with tool calls."""
    reply = reply_from_response(
        response(tool_calls=[("c1", "read_file", '{"path": "x.py"}')], finish="stop"),
        model="m", latency_ms=1, cost_usd=None,
    )
    assert reply.stop_reason == "tool_use"
    assert reply.wants_tools
    assert reply.tool_calls[0].arguments == {"path": "x.py"}
    assert reply.content[-1] == {
        "type": "tool_use", "id": "c1", "name": "read_file", "input": {"path": "x.py"}
    }


def test_unparseable_arguments_reach_the_tool_as_an_error_not_as_nothing():
    reply = reply_from_response(
        response(tool_calls=[("c1", "read_file", '{"path": ')]),
        model="m", latency_ms=1, cost_usd=None,
    )
    assert "_unparseable_arguments" in reply.tool_calls[0].arguments


def test_finish_reasons_map_onto_the_loop_vocabulary():
    for finish, expected in [("stop", "end_turn"), ("length", "max_tokens"),
                             ("content_filter", "refusal")]:
        r = reply_from_response(response(text="x", finish=finish), model="m",
                                latency_ms=1, cost_usd=None)
        assert r.stop_reason == expected


def test_input_tokens_are_reported_uncached():
    """LiteLLM folds cache reads and writes into prompt_tokens; take them out."""
    r = reply_from_response(
        response(text="x", prompt=10_000, completion=5,
                 cache_read_input_tokens=7_000, cache_creation_input_tokens=2_000),
        model="m", latency_ms=1, cost_usd=None,
    )
    assert r.usage.input_tokens == 1_000
    assert r.usage.cache_read_tokens == 7_000
    assert r.usage.cache_write_tokens == 2_000
    assert r.usage.prompt_tokens == 10_000


def test_openai_style_cached_tokens_are_read_too():
    r = reply_from_response(
        response(text="x", prompt=500,
                 prompt_tokens_details=SimpleNamespace(cached_tokens=300)),
        model="m", latency_ms=1, cost_usd=None,
    )
    assert (r.usage.input_tokens, r.usage.cache_read_tokens) == (200, 300)


# ------------------------------------------------------------------- client


@pytest.fixture
def wire(monkeypatch):
    sent: list[dict] = []
    replies: list = []

    async def fake(**params):
        sent.append(params)
        item = replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(litellm, "acompletion", fake)
    monkeypatch.setattr(litellm, "completion_cost", lambda **kw: 0.0)
    monkeypatch.setattr(llm.asyncio, "sleep", _no_sleep)
    return sent, replies


async def _no_sleep(_):
    return None


async def test_anthropic_requests_carry_the_breakpoint_and_groq_requests_do_not(wire):
    sent, replies = wire
    replies += [response(text="a"), response(text="b")]
    system = Conversation(system_prompt="SYS").system_blocks()
    await LLMClient(model="anthropic/claude-sonnet-5").chat([{"role": "user", "content": "hi"}], system=system)
    await LLMClient(model="groq/llama-3.3-70b-versatile").chat([{"role": "user", "content": "hi"}], system=system)
    assert sent[0]["messages"][0]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert isinstance(sent[1]["messages"][0]["content"], str)


async def test_tools_are_sent_in_function_format(wire):
    sent, replies = wire
    replies.append(response(text="ok"))
    from codepilot.tools import schemas

    await LLMClient(model="groq/x").chat([{"role": "user", "content": "hi"}], tools=schemas(["finish"]))
    assert sent[0]["tools"][0]["type"] == "function"
    assert sent[0]["tools"][0]["function"]["name"] == "finish"


async def test_a_rate_limit_is_retried_and_then_succeeds(wire):
    sent, replies = wire
    replies += [
        litellm.RateLimitError("slow down", llm_provider="groq", model="x"),
        response(text="done"),
    ]
    reply = await LLMClient(model="groq/x").chat([{"role": "user", "content": "hi"}])
    assert reply.text == "done"
    assert len(sent) == 2


async def test_a_request_too_large_for_the_tier_is_not_retried(wire):
    sent, replies = wire
    replies.append(
        litellm.RateLimitError("Request too large for model: TPM limit 12000", llm_provider="groq", model="x")
    )
    with pytest.raises(LLMError, match="Request too large"):
        await LLMClient(model="groq/x").chat([{"role": "user", "content": "hi"}])
    assert len(sent) == 1


async def test_a_missing_model_is_an_llm_error_naming_it(wire):
    _, replies = wire
    replies.append(litellm.NotFoundError("no such model", llm_provider="groq", model="x"))
    with pytest.raises(LLMError, match="groq/gone"):
        await LLMClient(model="groq/gone").chat([{"role": "user", "content": "hi"}])


async def test_a_rejected_key_is_an_llm_error(wire):
    _, replies = wire
    replies.append(litellm.AuthenticationError("bad key", llm_provider="groq", model="x"))
    with pytest.raises(LLMError, match="rejected"):
        await LLMClient(model="groq/x").chat([{"role": "user", "content": "hi"}])


async def test_validate_names_the_missing_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(LLMError, match="GROQ_API_KEY"):
        await LLMClient(model="groq/llama-3.3-70b-versatile").validate()


def test_the_fallback_price_table_prices_cache_reads():
    full = llm.price_of("claude-opus-5", 1000, 0)
    cached = llm.price_of("claude-opus-5", 0, 0, cache_read_tokens=1000)
    assert cached < full
    assert llm.price_of("nobody/knows-this-model", 1, 1) is None


def test_the_cli_names_the_key_the_chosen_model_needs(tmp_path, monkeypatch, capsys):
    """Not ANTHROPIC_API_KEY for a Groq model: the message must point at the
    variable that would actually fix it."""
    from codepilot.cli import main

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    code = main(["-C", str(tmp_path), "run", "--model", "groq/llama-3.3-70b-versatile", "x"])
    assert code == 2
    assert "GROQ_API_KEY" in capsys.readouterr().out


# ------------------------------------------------- local models (Ollama)
#
# Reproduced against the real server before the guard existed: a ~26k-token
# prompt sent to qwen2.5:7b with num_ctx=16384 came back with no error,
# prompt_tokens=8194, and an answer that had lost the system prompt — Ollama
# truncates silently. A run would have scored that as the model failing.


async def test_provider_options_such_as_num_ctx_are_passed_through(wire):
    sent, replies = wire
    replies.append(response(text="ok"))
    await LLMClient(model="ollama/qwen2.5:7b", extra={"num_ctx": 16384}).chat(
        [{"role": "user", "content": "hi"}], max_tokens=100
    )
    assert sent[0]["num_ctx"] == 16384


async def test_a_prompt_that_cannot_fit_num_ctx_is_refused_not_truncated(wire):
    sent, _ = wire
    big = "the quick brown fox jumps over the lazy dog " * 2000
    with pytest.raises(LLMError, match="context overflow"):
        await LLMClient(model="ollama/qwen2.5:7b", extra={"num_ctx": 4096}).chat(
            [{"role": "user", "content": big}], max_tokens=512
        )
    assert sent == [], "the request must not be sent"


async def test_ollama_models_use_the_native_chat_endpoint(wire):
    """Found by the local smoke run (MERGE_DECISIONS D24): LiteLLM's `ollama/`
    route goes through /api/generate and *emulates* tool calls by forcing JSON
    output and accepting only a top-level {"name", "arguments"} object. In 3 of
    4 agent runs qwen2.5:7b answered in the nested OpenAI shape it had seen in
    its own history ({"id", "type": "function", "function": {...}}), which that
    parser passes through as plain text, so the loop saw no tool call and
    stopped. `ollama_chat/` is Ollama's native /api/chat, which parses tool
    calls itself."""
    sent, replies = wire
    replies.append(response(text="ok"))
    from codepilot.tools import schemas

    await LLMClient(model="ollama/qwen2.5:7b").chat(
        [{"role": "user", "content": "hi"}], tools=schemas(["finish"])
    )
    assert sent[0]["model"] == "ollama_chat/qwen2.5:7b"


# ------------------------------------------------------------------ ledger


async def test_every_call_is_in_the_ledger_before_chat_returns(wire, tmp_path):
    from codepilot.llm import Ledger

    sent, replies = wire
    replies += [response(text="a", prompt=1000, completion=10), litellm.BadRequestError("bad", model="x", llm_provider="anthropic")]
    ledger = Ledger(tmp_path / "ledger.jsonl")
    client = LLMClient(model="claude-haiku-4-5", ledger=ledger)
    client.tag = "inst:agent"
    await client.chat([{"role": "user", "content": "hi"}], max_tokens=10)
    rows = ledger.rows()
    assert len(rows) == 1 and rows[0]["tag"] == "inst:agent"
    assert rows[0]["cost_usd"] == pytest.approx(1000 * 1e-6 + 10 * 5e-6)
    with pytest.raises(LLMError):
        await client.chat([{"role": "user", "content": "hi"}], max_tokens=10)
    rows = ledger.rows()
    assert len(rows) == 2 and "BadRequestError" in rows[1]["error"]
    assert ledger.total_usd("inst:") == pytest.approx(rows[0]["cost_usd"])
    assert client.spent("inst:").calls == 1


# ---------------------------------------------------- sampling and 4xx (D29)
#
# Reproduced from the installed LiteLLM: get_supported_openai_params lists
# `temperature` as supported for claude-opus-5-5 (which rejects it with a 400)
# and does not list `seed` for Anthropic or Gemini, so drop_params removed the
# seed silently while the run records claimed it was seeded.


async def test_models_that_reject_sampling_params_are_not_sent_them(wire):
    sent, replies = wire
    replies += [response(text="a"), response(text="b"), response(text="c")]
    opus = await LLMClient(model="claude-opus-5-5").chat(
        [{"role": "user", "content": "hi"}], temperature=0.2, seed=7, max_tokens=10)
    assert "temperature" not in sent[0] and "seed" not in sent[0]
    assert opus.omitted_params == ["temperature", "seed"]
    haiku = await LLMClient(model="claude-haiku-4-5").chat(
        [{"role": "user", "content": "hi"}], temperature=0.2, seed=7, max_tokens=10)
    assert sent[1]["temperature"] == 0.2 and "seed" not in sent[1]
    assert haiku.omitted_params == ["seed"]
    groq = await LLMClient(model="groq/llama-3.3-70b-versatile").chat(
        [{"role": "user", "content": "hi"}], temperature=0.2, seed=7, max_tokens=10)
    assert sent[2]["temperature"] == 0.2 and sent[2]["seed"] == 7
    assert groq.omitted_params == []


@pytest.mark.parametrize("error", [
    litellm.BadRequestError("bad request", model="x", llm_provider="anthropic"),
    litellm.UnprocessableEntityError("unprocessable", model="x", llm_provider="anthropic",
                                     response=__import__("httpx").Response(422, request=__import__("httpx").Request("POST", "http://x"))),
])
async def test_a_rejected_request_aborts_the_run_instead_of_failing_the_agent(wire, error):
    from codepilot.llm import AbortRun

    sent, replies = wire
    replies.append(error)
    with pytest.raises(AbortRun):
        await LLMClient(model="claude-haiku-4-5").chat([{"role": "user", "content": "hi"}], max_tokens=10)
    assert len(sent) == 1, "a 4xx is not retried"


async def test_a_persistent_rate_limit_costs_at_most_two_requests(wire):
    """D30: the client's own retry loop allowed 6 retries (7 requests), and the
    OpenAI SDK under LiteLLM's openai-compatible routes retries twice more by
    default, so one call could become many. Now: two requests at most, one
    retry layer."""
    sent, replies = wire
    replies += [litellm.RateLimitError("slow down", llm_provider="groq", model="x") for _ in range(5)]
    with pytest.raises(litellm.RateLimitError):
        await LLMClient(model="groq/x").chat([{"role": "user", "content": "hi"}])
    assert len(sent) == 2
    assert sent[0]["num_retries"] == 0 and sent[0]["max_retries"] == 0


# ------------------------------------------------- thinking blocks (D31)
#
# Claude 5-family models think by default (adaptive) and return `thinking`
# blocks — usually with empty text and a signature — before their tool_use
# blocks. The Anthropic API requires them to be passed back unchanged on the
# next request of a tool-use turn. LiteLLM exposes them as
# `message.thinking_blocks` and accepts them back on an assistant message
# under the same key. Reproduced: reply_from_response dropped them, so the
# second request of every agent turn on Opus/Sonnet 5.5 went out without them.


def claude_response_with_thinking():
    from types import SimpleNamespace

    thinking = [{"type": "thinking", "thinking": "", "signature": "sig-abc"},
                {"type": "redacted_thinking", "data": "opaque"}]
    call = SimpleNamespace(id="toolu_1", function=SimpleNamespace(name="read_file", arguments='{"path": "a.py"}'))
    return SimpleNamespace(
        model="claude-sonnet-5-5",
        choices=[SimpleNamespace(
            message=SimpleNamespace(content="Reading it.", tool_calls=[call], thinking_blocks=thinking,
                                    reasoning_content=""),
            finish_reason="tool_calls")],
        usage=SimpleNamespace(prompt_tokens=6000, completion_tokens=120,
                              cache_read_input_tokens=4000, cache_creation_input_tokens=1500),
    )


async def test_thinking_blocks_round_trip_with_tool_calls_and_cache_usage(wire):
    sent, replies = wire
    replies += [
        claude_response_with_thinking(),
        litellm.RateLimitError("slow down", llm_provider="anthropic", model="x"),
        response(text="done"),
        litellm.BadRequestError("thinking block missing", model="x", llm_provider="anthropic"),
    ]
    from codepilot.llm import AbortRun

    client = LLMClient(model="claude-sonnet-5-5")
    convo = Conversation(system_prompt="SYS")
    convo.user("fix it")
    first = await client.chat(convo.messages, system=convo.system_blocks(), max_tokens=100)
    assert [b["type"] for b in first.content] == ["thinking", "redacted_thinking", "text", "tool_use"]
    assert first.content[0]["signature"] == "sig-abc"
    assert (first.usage.input_tokens, first.usage.cache_read_tokens, first.usage.cache_write_tokens) == (500, 4000, 1500)
    assert first.cost_usd == pytest.approx(500 * 2e-6 + 120 * 10e-6 + 4000 * 0.2e-6 + 1500 * 2.5e-6)

    convo.assistant(first.content)
    convo.tool_results([Conversation.tool_result("toolu_1", "contents")])
    await client.chat(convo.messages, system=convo.system_blocks(), max_tokens=100)  # 429, then ok
    echoed = [m for m in sent[1]["messages"] if m["role"] == "assistant"][0]
    assert echoed["thinking_blocks"] == [
        {"type": "thinking", "thinking": "", "signature": "sig-abc"},
        {"type": "redacted_thinking", "data": "opaque"},
    ]
    assert echoed["tool_calls"][0]["id"] == "toolu_1"
    assert len(sent) == 3, "the 429 was retried once"
    with pytest.raises(AbortRun):
        await client.chat(convo.messages, system=convo.system_blocks(), max_tokens=100)


def test_thinking_blocks_are_not_sent_to_providers_that_did_not_make_them():
    msgs = [{"role": "assistant", "content": [
        {"type": "thinking", "thinking": "", "signature": "s"},
        {"type": "text", "text": "hi"}]}]
    client = LLMClient(model="groq/llama-3.3-70b-versatile")
    params = client._request(msgs, system=None, tools=None, model=client.model, max_tokens=10,
                             temperature=None, effort=None)
    assert "thinking_blocks" not in params["messages"][0]


def test_litellm_puts_echoed_thinking_ahead_of_the_tool_call_in_the_anthropic_body():
    """Not just our translation: LiteLLM 1.103.2's own Anthropic transform,
    offline, produces thinking -> tool_use in the assistant turn."""
    from litellm.llms.anthropic.chat.transformation import AnthropicConfig

    convo = Conversation(system_prompt="S")
    convo.user("fix")
    convo.assistant([{"type": "thinking", "thinking": "", "signature": "sig"},
                     {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a"}}])
    convo.tool_results([Conversation.tool_result("toolu_1", "x")])
    body = AnthropicConfig().transform_request(
        model="claude-sonnet-5-5", messages=to_openai_messages(convo.messages),
        optional_params={"max_tokens": 10}, litellm_params={}, headers={})
    assistant = [m for m in body["messages"] if m["role"] == "assistant"][0]
    assert [b["type"] for b in assistant["content"]] == ["thinking", "tool_use"]
    assert assistant["content"][0]["signature"] == "sig"
