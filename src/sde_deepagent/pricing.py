"""LLM pricing table and per-task cost tracking.

Prices are USD per million tokens (input, output), current as of 2026-06.
They WILL drift — override any entry under `pricing:` in config/agents.yaml
without touching code. Unknown models are tracked token-wise but priced at $0
and flagged, so budget enforcement never silently miscounts a priced model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# model-name prefix -> (input $/MTok, output $/MTok); longest prefix wins
DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-4": (5.0, 25.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4": (1.0, 5.0),
    # Google
    "gemini-3.5-flash": (1.50, 9.0),
    "gemini-3.1-pro": (2.0, 12.0),
    "gemini-3-flash": (0.50, 3.0),
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    # OpenAI
    "gpt-5.5-pro": (30.0, 180.0),
    "gpt-5.5": (5.0, 30.0),
    "gpt-5.4": (2.50, 15.0),
    "gpt-5-mini": (0.25, 2.0),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5": (1.25, 10.0),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4o": (2.50, 10.0),
    "o4-mini": (1.10, 4.40),
    "o3": (2.0, 8.0),
}

# Cache token cost relative to the input price (estimates; provider-published):
# Anthropic: reads ~0.1x, writes ~1.25x. Google: cached reads ~0.25x.
# OpenAI: cached reads ~0.1x on the gpt-5 line, ~0.25x elsewhere; no write fee.
CACHE_MULTIPLIERS = {
    "claude": {"read": 0.10, "write": 1.25},
    "gemini": {"read": 0.25, "write": 1.0},
    "gpt-5": {"read": 0.10, "write": 1.0},
    "gpt": {"read": 0.25, "write": 1.0},
    "o": {"read": 0.25, "write": 1.0},
}


def normalize_model_name(name: str) -> str:
    """'anthropic:claude-sonnet-4-6' / 'models/gemini-2.5-pro' -> bare name."""
    if ":" in name:
        name = name.split(":", 1)[1]
    if name.startswith("models/"):
        name = name[len("models/"):]
    return name.strip()


def lookup_price(model: str, overrides: dict[str, dict] | None = None
                 ) -> tuple[float, float] | None:
    """Return (input, output) $/MTok for a model, or None if unpriced."""
    name = normalize_model_name(model)
    table = dict(DEFAULT_PRICING)
    for key, spec in (overrides or {}).items():
        if isinstance(spec, dict) and "input" in spec and "output" in spec:
            table[normalize_model_name(key)] = (float(spec["input"]), float(spec["output"]))
    for prefix in sorted(table, key=len, reverse=True):
        if name.startswith(prefix):
            return table[prefix]
    return None


def _cache_multipliers(model: str) -> dict[str, float]:
    name = normalize_model_name(model)
    for prefix in sorted(CACHE_MULTIPLIERS, key=len, reverse=True):
        if name.startswith(prefix):
            return CACHE_MULTIPLIERS[prefix]
    return {"read": 1.0, "write": 1.0}


class BudgetExceeded(RuntimeError):
    def __init__(self, spent: float, limit: float):
        self.spent, self.limit = spent, limit
        super().__init__(f"task budget exceeded: ${spent:.4f} spent, limit ${limit:.2f}")


@dataclass
class CostTracker:
    """Accumulates token usage and dollar cost across one agent run."""

    default_model: str
    overrides: dict[str, dict] | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    unpriced_models: set[str] = field(default_factory=set)
    budget_warned: bool = False  # set once the 80% warning has been emitted

    def add_usage(self, usage_metadata: dict, model_name: str | None = None) -> float:
        """Record one model response's usage_metadata. Returns the cost delta."""
        model = model_name or self.default_model
        in_tok = int(usage_metadata.get("input_tokens") or 0)
        out_tok = int(usage_metadata.get("output_tokens") or 0)
        self.input_tokens += in_tok
        self.output_tokens += out_tok

        price = lookup_price(model, self.overrides)
        if price is None:
            self.unpriced_models.add(normalize_model_name(model))
            return 0.0
        p_in, p_out = price
        details = usage_metadata.get("input_token_details") or {}
        # Cache tokens are a subset of input_tokens. Clamp so a malformed usage
        # report (cache_read + cache_write > input_tokens) can't bill for more
        # tokens than were actually input and blow through the budget.
        cache_read = min(int(details.get("cache_read") or 0), in_tok)
        cache_write = min(int(details.get("cache_creation") or 0), in_tok - cache_read)
        plain_in = max(0, in_tok - cache_read - cache_write)
        mult = _cache_multipliers(model)
        delta = (
            plain_in * p_in
            + cache_read * p_in * mult["read"]
            + cache_write * p_in * mult["write"]
            + out_tok * p_out
        ) / 1_000_000
        self.cost_usd += delta
        return delta

    def summary(self) -> dict:
        out = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd, 6),
        }
        if self.unpriced_models:
            out["unpriced_models"] = sorted(self.unpriced_models)
        return out
