"""
Provider + model registry for BYOK (bring-your-own-key) multi-provider support.

Single source of truth for which providers/models are offered, how to build
the LiteLLM model string, and which env var holds a key for local runs.
`codepilot.llm` accepts any LiteLLM model string; this registry is what the
CLI's `--provider` shorthand and the benchmark runner resolve against.

`free_tier` marks providers with a no-cost API tier (Groq, Google AI Studio).
Whether a given key is actually on that tier is a property of the account, not
of the key string, and cannot be checked from here.

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
    free_tier: bool = False


# Curated, tool-capable defaults per provider. The first model listed is the
# default for eval and recording runs, so it is the one kept verified.
#
# Model ids drift faster than any repo can track. The dropdown is a convenience
# only: BYOK passes whatever id you give it straight through to the provider, so
# a stale entry here costs a dropdown row, never the ability to run.
PROVIDERS: dict[str, Provider] = {
    "anthropic": Provider(
        key="anthropic",
        label="Anthropic",
        litellm_prefix="anthropic",
        key_env="ANTHROPIC_API_KEY",
        key_url="https://console.anthropic.com/settings/keys",
        models=(
            Model("claude-sonnet-5", "Claude Sonnet 5"),
            Model("claude-opus-5", "Claude Opus 5"),
            Model("claude-haiku-4-5", "Claude Haiku 4.5 (fast/cheap)"),
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
        free_tier=True,
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
        free_tier=True,
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


def provider_for_model(model: str) -> Provider | None:
    """The registry entry a LiteLLM model string belongs to, if any.

    A bare Claude id routes to Anthropic, as LiteLLM routes it.
    """
    prefix = model.split("/", 1)[0] if "/" in model else ""
    for p in PROVIDERS.values():
        if prefix == p.litellm_prefix:
            return p
    if model.startswith("claude"):
        return PROVIDERS["anthropic"]
    if model.startswith(("gpt-", "o1", "o3", "o4")):
        return PROVIDERS["openai"]
    return None


def key_env_for_model(model: str) -> str | None:
    """Env var holding the key a model string needs, or None if unknown."""
    p = provider_for_model(model)
    return p.key_env if p else None


def providers_payload() -> list[dict]:
    """Serialize the registry for a UI's dropdowns."""
    return [
        {
            "key": p.key,
            "label": p.label,
            "keyUrl": p.key_url,
            "models": [{"id": m.id, "label": m.label} for m in p.models],
        }
        for p in PROVIDERS.values()
    ]
