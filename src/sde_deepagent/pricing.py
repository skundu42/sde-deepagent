"""LLM pricing table and per-task cost tracking.

Prices are USD per million tokens (input, output), current as of 2026-06.
They WILL drift — override any entry under `pricing:` in config/agents.yaml
without touching code. Unknown models are flagged AND charged the priciest known
rate (see `fallback_price`), so an unpriced or typo'd model id can never slip
past the per-task and daily budgets by counting as $0.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db import Database

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


def _merged_table(overrides: dict[str, dict] | None = None
                  ) -> dict[str, tuple[float, float]]:
    """Default price table with any config `pricing:` overrides merged in.
    Malformed or negative overrides are skipped (with a warning) rather than
    crashing a task mid-run or silently producing a bogus (e.g. negative) cost."""
    table = dict(DEFAULT_PRICING)
    for key, spec in (overrides or {}).items():
        if not (isinstance(spec, dict) and "input" in spec and "output" in spec):
            continue
        try:
            p_in, p_out = float(spec["input"]), float(spec["output"])
        except (TypeError, ValueError):
            logger.warning("ignoring malformed pricing override for %r: %r", key, spec)
            continue
        if p_in < 0 or p_out < 0:
            logger.warning("ignoring negative pricing override for %r: %r", key, spec)
            continue
        table[normalize_model_name(key)] = (p_in, p_out)
    return table


def lookup_price(model: str, overrides: dict[str, dict] | None = None
                 ) -> tuple[float, float] | None:
    """Return (input, output) $/MTok for a model, or None if unpriced."""
    name = normalize_model_name(model)
    table = _merged_table(overrides)
    for prefix in sorted(table, key=len, reverse=True):
        if name.startswith(prefix):
            return table[prefix]
    return None


def fallback_price(overrides: dict[str, dict] | None = None) -> tuple[float, float]:
    """Fail-safe price for a model absent from the table: the priciest known
    input and output rates. An unknown/typo'd model id is then charged the
    dearest plausible rate, so the per-task and daily budgets still bound it
    (over-estimating cost is safe; pricing it at $0 would let it run unmetered).

    Floored at the dearest *default* rate so that overriding an expensive model
    to be cheap cannot lower the fail-safe ceiling (overrides can only raise it)."""
    table = _merged_table(overrides)
    max_in = max(max(p[0] for p in DEFAULT_PRICING.values()),
                 max(p[0] for p in table.values()))
    max_out = max(max(p[1] for p in DEFAULT_PRICING.values()),
                  max(p[1] for p in table.values()))
    return (max_in, max_out)


def _cache_multipliers(model: str) -> dict[str, float]:
    name = normalize_model_name(model)
    for prefix in sorted(CACHE_MULTIPLIERS, key=len, reverse=True):
        if name.startswith(prefix):
            return CACHE_MULTIPLIERS[prefix]
    return {"read": 1.0, "write": 1.0}


class BudgetExceeded(RuntimeError):
    kind = "task"

    def __init__(self, spent: float, limit: float):
        self.spent, self.limit = spent, limit
        super().__init__(f"{self.kind} budget exceeded: ${spent:.4f} spent, limit ${limit:.2f}")


class DailyBudgetExceeded(BudgetExceeded):
    """Raised when the account-wide daily cap is reached mid-task, so the run
    aborts instead of letting cumulative spend drift past the ceiling."""

    kind = "daily"


@dataclass
class CostTracker:
    """Accumulates token usage and dollar cost across one agent run."""

    default_model: str
    overrides: dict[str, dict] | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    # portion of cost_usd already written to the DB (mid-run flushes). The
    # daily accountant counts only the delta above this, so a flushed run's
    # spend is never double-counted (once from the DB row, once live).
    persisted_usd: float = 0.0
    unpriced_models: set[str] = field(default_factory=set)
    budget_warned: bool = False  # set once the 80% warning has been emitted
    unpriced_warned: bool = False  # set once the unpriced-model warning has fired

    def add_usage(self, usage_metadata: dict, model_name: str | None = None) -> float:
        """Record one model response's usage_metadata. Returns the cost delta."""
        model = model_name or self.default_model
        in_tok = int(usage_metadata.get("input_tokens") or 0)
        out_tok = int(usage_metadata.get("output_tokens") or 0)
        self.input_tokens += in_tok
        self.output_tokens += out_tok

        price = lookup_price(model, self.overrides)
        if price is None:
            # fail-safe: flag it AND charge the priciest known rate so the budget
            # still bounds it, rather than letting an unpriced model run unmetered
            self.unpriced_models.add(normalize_model_name(model))
            price = fallback_price(self.overrides)
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


def utc_midnight_ts() -> float:
    """Start of the current UTC day, as a POSIX timestamp."""
    now = dt.datetime.now(dt.timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class DailyBudget:
    """Account-wide daily LLM-spend cap, enforced as a HARD ceiling.

    Persisted spend (``db.spend_since``) only reflects *finished* work — an
    in-flight task hasn't written its cost yet. Gating solely on persisted spend
    lets several concurrent runs each pass the check and collectively overshoot
    the cap. This accountant also sums the live, not-yet-persisted cost of every
    running task, so both the dispatcher's launch gate and the runner's
    mid-stream check see the true total. One instance is shared by the worker
    and the runner.
    """

    def __init__(self, db: Database, limit_usd: float) -> None:
        self.db = db
        self.limit_usd = limit_usd
        self._live: dict[str, CostTracker] = {}

    def track(self, task_id: str, tracker: CostTracker) -> None:
        """Register a running task's tracker so its spend counts immediately."""
        self._live[task_id] = tracker

    def untrack(self, task_id: str) -> None:
        self._live.pop(task_id, None)

    def live_usd(self) -> float:
        """UNPERSISTED spend of every in-flight run: only the delta above what
        each tracker has already flushed to the DB, which spend_since() is
        counting from the task/chat rows."""
        return sum(max(0.0, t.cost_usd - t.persisted_usd)
                   for t in self._live.values())

    async def spent_usd(self) -> float:
        """Persisted spend today + live spend of all in-flight tasks."""
        return await self.db.spend_since(utc_midnight_ts()) + self.live_usd()
