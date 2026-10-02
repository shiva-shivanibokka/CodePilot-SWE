"""The provider registry and the tool-schema conversion, ported from
Autonomous-SWE-Agent's tests/test_providers.py.

B's `assistant_message` and `LLMConfig` tests are not ported: both types were
removed with B's client. Their contracts — tool calls serialised as JSON
argument strings, ids preserved — are tested on the merged client in
tests/test_llm.py::test_tool_use_and_tool_result_blocks_become_openai_tool_messages.
"""

from __future__ import annotations

import pytest

from codepilot.llm import to_openai_tools
from codepilot.providers import (
    PROVIDERS,
    key_env_for,
    key_env_for_model,
    litellm_model,
    providers_payload,
)


class TestRegistry:
    def test_expected_providers_present(self):
        assert set(PROVIDERS) == {"anthropic", "openai", "google", "groq"}

    def test_every_provider_has_models(self):
        for p in PROVIDERS.values():
            assert p.models, f"{p.key} has no models"
            assert p.key_env and p.key_url

    def test_model_ids_unique_within_provider(self):
        for p in PROVIDERS.values():
            ids = [m.id for m in p.models]
            assert len(ids) == len(set(ids)), f"duplicate model id in {p.key}"

    def test_the_free_tier_providers_are_marked(self):
        assert {k for k, p in PROVIDERS.items() if p.free_tier} == {"google", "groq"}


class TestLitellmModel:
    def test_prefixes_by_provider(self):
        assert litellm_model("anthropic", "claude-opus-4-8") == "anthropic/claude-opus-4-8"
        # Google routes through the "gemini" prefix, not "google".
        assert litellm_model("google", "gemini-3.1-pro") == "gemini/gemini-3.1-pro"
        assert litellm_model("groq", "llama-3.3-70b-versatile") == "groq/llama-3.3-70b-versatile"

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError):
            litellm_model("cohere", "whatever")

    def test_key_env_for(self):
        assert key_env_for("openai") == "OPENAI_API_KEY"

    def test_key_env_for_a_model_string(self):
        assert key_env_for_model("groq/llama-3.3-70b-versatile") == "GROQ_API_KEY"
        assert key_env_for_model("gemini/gemini-2.5-flash") == "GEMINI_API_KEY"
        assert key_env_for_model("claude-opus-5") == "ANTHROPIC_API_KEY"
        assert key_env_for_model("ollama/llama3") is None


class TestProvidersPayload:
    def test_shape(self):
        payload = providers_payload()
        assert isinstance(payload, list) and payload
        for entry in payload:
            assert set(entry) == {"key", "label", "keyUrl", "models"}
            for m in entry["models"]:
                assert set(m) == {"id", "label"}


class TestToOpenAITools:
    def test_converts_anthropic_style_schema(self):
        anthropic_style = [
            {
                "name": "bash",
                "description": "run a command",
                "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}},
            }
        ]
        out = to_openai_tools(anthropic_style)
        assert out[0]["type"] == "function"
        fn = out[0]["function"]
        assert fn["name"] == "bash"
        assert fn["description"] == "run a command"
        assert fn["parameters"]["properties"]["command"]["type"] == "string"

    def test_missing_input_schema_defaults_to_empty_object(self):
        out = to_openai_tools([{"name": "noop"}])
        assert out[0]["function"]["parameters"] == {"type": "object", "properties": {}}
