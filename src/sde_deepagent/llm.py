"""Model factory. Supports Anthropic (Claude), Google (Gemini) and OpenAI (GPT /
o-series) via LangChain's init_chat_model. Accepts either "provider:model"
identifiers or bare model names which are auto-prefixed."""

from __future__ import annotations

import re

from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel

KNOWN_PROVIDERS = ("anthropic", "google_genai", "openai")

# Curated picks for the UI dropdowns. Any other model from a known provider
# still works via config/agents.yaml — this is convenience, not a whitelist.
KNOWN_MODELS: dict[str, list[str]] = {
    "anthropic": [
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
    ],
    "google_genai": [
        "gemini-3.1-pro-preview",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
    ],
    "openai": [
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "gpt-5-mini",
        "gpt-4.1",
        "o3",
        "o4-mini",
    ],
}

O_SERIES_RE = re.compile(r"^o\d")  # o1, o3, o4-mini, ...


def normalize_model_id(model: str) -> str:
    model = model.strip()
    if ":" in model:
        provider, name = model.split(":", 1)
        if provider not in KNOWN_PROVIDERS:
            raise ValueError(
                f"Unsupported provider '{provider}'. Use one of: {', '.join(KNOWN_PROVIDERS)}"
            )
        return f"{provider}:{name}"
    if model.startswith("claude"):
        return f"anthropic:{model}"
    if model.startswith("gemini"):
        return f"google_genai:{model}"
    if model.startswith("gpt") or O_SERIES_RE.match(model):
        return f"openai:{model}"
    raise ValueError(
        f"Cannot infer provider for model '{model}'. "
        "Use 'anthropic:<model>', 'google_genai:<model>' or 'openai:<model>'."
    )


EFFORT_LEVELS = ("low", "medium", "high")


def build_model(model: str, max_tokens: int = 16000,
                effort: str | None = None) -> BaseChatModel:
    """Construct a chat model, optionally with a reasoning-effort level.

    Effort maps to each provider's own knob (verified against the installed
    integrations): OpenAI `reasoning_effort`, Anthropic `effort`, Gemini
    `thinking_level` (which only has low/high — medium rounds up)."""
    model_id = normalize_model_id(model)
    kwargs: dict = {"max_tokens": max_tokens}
    if effort:
        if effort not in EFFORT_LEVELS:
            raise ValueError(f"effort must be one of {EFFORT_LEVELS}, got '{effort}'")
        provider = model_id.split(":", 1)[0]
        if provider == "openai":
            kwargs["reasoning_effort"] = effort
            # observed live: chat/completions rejects reasoning_effort combined
            # with function tools on gpt-5.x — the Responses API is required
            kwargs["use_responses_api"] = True
        elif provider == "anthropic":
            kwargs["effort"] = effort
        elif provider == "google_genai":
            kwargs["thinking_level"] = "low" if effort == "low" else "high"
    return init_chat_model(model_id, **kwargs)
