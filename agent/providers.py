"""
Provider + model registry for BYOK (bring-your-own-key) multi-provider support.

Single source of truth for which providers/models the UI offers, how to build
the LiteLLM model string, and which env var holds a key for local eval runs.
The API exposes this to the frontend via GET /providers so the dropdowns are
server-driven — edit this file and both backend and frontend stay in sync.

Model lists drift. Verify current IDs at:
  Anthropic  https://docs.anthropic.com/en/docs/about-claude/models
  OpenAI     https://platform.openai.com/docs/models
  Google     https://ai.google.dev/gemini-api/docs/models
  Groq       https://console.groq.com/docs/models
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Model:
    id: str  # provider-native model id passed to LiteLLM after the route prefix
    label: str  # human label shown in the dropdown


@dataclass(frozen=True)
class Provider:
    key: str  # "anthropic" | "openai" | "google" | "groq"
    label: str
    litellm_prefix: str  # LiteLLM route prefix, e.g. "gemini" for Google
    key_env: str  # env var used as the key for local eval runs
    key_url: str  # where a user gets an API key (shown in the UI)
    models: tuple[Model, ...]


# Curated, tool-capable defaults per provider (current as of 2026-07).
# The dropdown is a convenience — BYOK still accepts any model the key supports.
PROVIDERS: dict[str, Provider] = {
    "anthropic": Provider(
        key="anthropic",
        label="Anthropic",
        litellm_prefix="anthropic",
        key_env="ANTHROPIC_API_KEY",
        key_url="https://console.anthropic.com/settings/keys",
        models=(
            Model("claude-opus-4-8", "Claude Opus 4.8"),
            Model("claude-sonnet-5", "Claude Sonnet 5"),
            Model("claude-haiku-4-5", "Claude Haiku 4.5"),
        ),
    ),
    "openai": Provider(
        key="openai",
        label="OpenAI",
        litellm_prefix="openai",
        key_env="OPENAI_API_KEY",
        key_url="https://platform.openai.com/api-keys",
        models=(
            Model("gpt-5.6-sol", "GPT-5.6 Sol (flagship)"),
            Model("gpt-5.6-terra", "GPT-5.6 Terra (balanced)"),
            Model("gpt-5.6-luna", "GPT-5.6 Luna (fast/cheap)"),
        ),
    ),
    "google": Provider(
        key="google",
        label="Google Gemini",
        litellm_prefix="gemini",
        key_env="GEMINI_API_KEY",
        key_url="https://aistudio.google.com/apikey",
        models=(
            Model("gemini-3.1-pro", "Gemini 3.1 Pro"),
            Model("gemini-3.5-flash", "Gemini 3.5 Flash"),
            Model("gemini-2.5-flash", "Gemini 2.5 Flash"),
        ),
    ),
    "groq": Provider(
        key="groq",
        label="Groq",
        litellm_prefix="groq",
        key_env="GROQ_API_KEY",
        key_url="https://console.groq.com/keys",
        models=(
            Model("llama-3.3-70b-versatile", "Llama 3.3 70B"),
            Model("openai/gpt-oss-120b", "GPT-OSS 120B"),
            Model("llama-3.1-8b-instant", "Llama 3.1 8B (instant)"),
        ),
    ),
}


def litellm_model(provider: str, model: str) -> str:
    """Build the LiteLLM model string, e.g. ('google', 'gemini-3.1-pro') -> 'gemini/gemini-3.1-pro'."""
    p = PROVIDERS.get(provider)
    if p is None:
        raise ValueError(f"Unknown provider {provider!r}. Options: {sorted(PROVIDERS)}")
    return f"{p.litellm_prefix}/{model}"


def key_env_for(provider: str) -> str:
    """Env var that holds an API key for this provider (used by local eval runs)."""
    p = PROVIDERS.get(provider)
    if p is None:
        raise ValueError(f"Unknown provider {provider!r}. Options: {sorted(PROVIDERS)}")
    return p.key_env


def providers_payload() -> list[dict]:
    """Serialize the registry for the frontend dropdowns (GET /providers)."""
    return [
        {
            "key": p.key,
            "label": p.label,
            "keyUrl": p.key_url,
            "models": [{"id": m.id, "label": m.label} for m in p.models],
        }
        for p in PROVIDERS.values()
    ]
