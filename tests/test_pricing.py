import pytest

from sde_deepagent.pricing import (
    BudgetExceeded, CostTracker, lookup_price, normalize_model_name,
)


@pytest.mark.parametrize("given,expected", [
    ("anthropic:claude-sonnet-4-6", "claude-sonnet-4-6"),
    ("models/gemini-2.5-pro", "gemini-2.5-pro"),
    ("claude-opus-4-8", "claude-opus-4-8"),
    ("google_genai:gemini-2.5-flash", "gemini-2.5-flash"),
])
def test_normalize_model_name(given, expected):
    assert normalize_model_name(given) == expected


def test_lookup_longest_prefix_wins():
    # flash-lite must not be priced as flash
    assert lookup_price("gemini-2.5-flash-lite") == (0.10, 0.40)
    assert lookup_price("gemini-2.5-flash") == (0.30, 2.50)
    # dated/suffixed model ids resolve via prefix
    assert lookup_price("claude-haiku-4-5-20251001") == (1.0, 5.0)
    assert lookup_price("anthropic:claude-sonnet-4-6") == (3.0, 15.0)
    # openai: 5.5/5.4/mini/nano must not collapse onto the bare gpt-5 price
    assert lookup_price("openai:gpt-5.5") == (5.0, 30.0)
    assert lookup_price("gpt-5.4") == (2.50, 15.0)
    assert lookup_price("gpt-5-mini") == (0.25, 2.0)
    assert lookup_price("gpt-5") == (1.25, 10.0)
    assert lookup_price("o4-mini") == (1.10, 4.40)


def test_openai_cache_read_discount():
    t = CostTracker(default_model="openai:gpt-5.4")
    t.add_usage({"input_tokens": 1_000_000, "output_tokens": 0,
                 "input_token_details": {"cache_read": 1_000_000}})
    # fully cached @ 0.1x of $2.50
    assert t.cost_usd == pytest.approx(0.25)


def test_lookup_unknown_and_overrides():
    assert lookup_price("mystery-model-9000") is None
    over = {"mystery-model": {"input": 1.0, "output": 2.0},
            "claude-sonnet-4-6": {"input": 99.0, "output": 99.0}}
    assert lookup_price("mystery-model-9000", over) == (1.0, 2.0)
    assert lookup_price("claude-sonnet-4-6", over) == (99.0, 99.0)


def test_cost_tracker_basic():
    t = CostTracker(default_model="anthropic:claude-sonnet-4-6")
    delta = t.add_usage({"input_tokens": 1_000_000, "output_tokens": 100_000})
    # 1M in @ $3 + 100k out @ $15 -> 3 + 1.5
    assert delta == pytest.approx(4.5)
    assert t.cost_usd == pytest.approx(4.5)
    assert t.input_tokens == 1_000_000 and t.output_tokens == 100_000


def test_cost_tracker_cache_discounts():
    t = CostTracker(default_model="claude-sonnet-4-6")
    t.add_usage({
        "input_tokens": 1_000_000, "output_tokens": 0,
        "input_token_details": {"cache_read": 800_000, "cache_creation": 100_000},
    })
    # 100k plain @ 3 + 800k read @ 0.3 + 100k write @ 3.75 -> 0.3 + 0.24 + 0.375
    assert t.cost_usd == pytest.approx(0.915)


def test_cost_tracker_per_message_model():
    t = CostTracker(default_model="anthropic:claude-sonnet-4-6")
    t.add_usage({"input_tokens": 1_000_000, "output_tokens": 0},
                model_name="gemini-2.5-flash")
    assert t.cost_usd == pytest.approx(0.30)  # priced as flash, not sonnet


def test_cost_tracker_unpriced_flagged_not_charged():
    t = CostTracker(default_model="weird:model-x")
    t.add_usage({"input_tokens": 5000, "output_tokens": 100})
    assert t.cost_usd == 0.0
    assert t.input_tokens == 5000
    assert "model-x" in t.summary()["unpriced_models"]


def test_budget_exceeded_message():
    err = BudgetExceeded(1.2345, 1.0)
    assert "1.2345" in str(err) and "1.00" in str(err)
