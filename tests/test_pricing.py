import pytest

from sde_deepagent.pricing import (
    DEFAULT_PRICING,
    BudgetExceeded,
    CostTracker,
    fallback_price,
    lookup_price,
    normalize_model_name,
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


def test_cache_tokens_clamped_to_input():
    # malformed/buggy upstream usage: cache tokens exceed reported input_tokens
    t = CostTracker(default_model="claude-sonnet-4-6")
    t.add_usage({
        "input_tokens": 1_000_000, "output_tokens": 0,
        "input_token_details": {"cache_read": 1_000_000, "cache_creation": 1_000_000},
    })
    # billed for at most the 1M input tokens (all as cache_read @ 0.1x of $3),
    # not the bogus 2M -> no runaway overcharge
    assert t.cost_usd == pytest.approx(0.30)


def test_cost_tracker_per_message_model():
    t = CostTracker(default_model="anthropic:claude-sonnet-4-6")
    t.add_usage({"input_tokens": 1_000_000, "output_tokens": 0},
                model_name="gemini-2.5-flash")
    assert t.cost_usd == pytest.approx(0.30)  # priced as flash, not sonnet


def test_cost_tracker_unpriced_charged_failsafe_and_flagged():
    # fail-safe: an unknown model must NOT be free, or it would slip past the
    # per-task and daily budgets entirely (a typo'd model id = unmetered spend).
    # It is charged the priciest known input/output rate — the budget then
    # over-estimates rather than under-counts — while still being flagged so the
    # operator knows to add real pricing.
    max_in = max(p[0] for p in DEFAULT_PRICING.values())
    max_out = max(p[1] for p in DEFAULT_PRICING.values())
    t = CostTracker(default_model="weird:model-x")
    delta = t.add_usage({"input_tokens": 5000, "output_tokens": 100})
    assert delta == pytest.approx((5000 * max_in + 100 * max_out) / 1_000_000)
    assert delta > 0
    assert t.input_tokens == 5000
    assert "model-x" in t.summary()["unpriced_models"]


def test_unpriced_failsafe_tracks_override_ceiling():
    # a pricey custom model in overrides raises the fail-safe ceiling too, so an
    # unknown model can never be cheaper to (mis)count than the dearest configured one
    over = {"ultra-expensive": {"input": 100.0, "output": 500.0}}
    t = CostTracker(default_model="brand-new-unknown", overrides=over)
    t.add_usage({"input_tokens": 1_000_000, "output_tokens": 0})
    assert t.cost_usd == pytest.approx(100.0)  # 1M input @ the $100/MTok override max


def test_malformed_and_negative_overrides_are_skipped_not_crashed():
    over = {"weird-x": {"input": "not_a_number", "output": 5.0},  # malformed
            "neg-y": {"input": -1.0, "output": 2.0},             # negative
            "ok-z": {"input": 4.0, "output": 8.0}}               # valid
    # known models still price normally; bad entries are ignored, not fatal
    assert lookup_price("claude-sonnet-4-6", over) == (3.0, 15.0)
    assert lookup_price("ok-z", over) == (4.0, 8.0)
    assert lookup_price("weird-x", over) is None  # malformed -> not registered
    assert lookup_price("neg-y", over) is None    # negative -> not registered
    # a bad override must not crash cost tracking for an unknown model
    t = CostTracker(default_model="weird-x", overrides=over)
    assert t.add_usage({"input_tokens": 1000, "output_tokens": 0}) > 0


def test_cheap_override_cannot_lower_failsafe_ceiling():
    base = fallback_price()
    # overriding the dearest model to be cheap must NOT lower the fail-safe ceiling
    cheap = {"gpt-5.5-pro": {"input": 0.01, "output": 0.01}}
    assert fallback_price(cheap) == base
    # but a pricier custom model DOES raise it
    assert fallback_price({"ultra": {"input": 999.0, "output": 1000.0}}) == (999.0, 1000.0)


def test_budget_exceeded_message():
    err = BudgetExceeded(1.2345, 1.0)
    assert "1.2345" in str(err) and "1.00" in str(err)
