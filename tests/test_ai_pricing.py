from decimal import Decimal

from amadeus_bot.services.ai_pricing import estimate_usage_cost_usd, provider_display_name


def test_deepseek_cost_uses_cache_and_peak_periods() -> None:
    cost = estimate_usage_cost_usd(
        {
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "input_tokens": 2_000_000,
            "output_tokens": 2_000_000,
            "cached_tokens": 1_000_000,
            "peak_input_tokens": 1_000_000,
            "peak_output_tokens": 1_000_000,
            "peak_cached_tokens": 500_000,
        }
    )

    assert cost == Decimal("2.3205")


def test_huanyan_cost_uses_discounted_price() -> None:
    cost = estimate_usage_cost_usd(
        {
            "provider": "huanyan",
            "model": "gpt-5.6-luna",
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "cached_tokens": 500_000,
        }
    )

    assert cost == Decimal("0.70")


def test_gpt_gateway_is_supported_as_huanyan_legacy_name() -> None:
    cost = estimate_usage_cost_usd(
        {
            "provider": "gpt_gateway",
            "model": "gpt-5.6-terra",
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
        }
    )

    assert cost == Decimal("1.75")
    assert provider_display_name("gpt_gateway") == "gpt_gateway（桓衍旧名）"


def test_unknown_model_has_no_cost_estimate() -> None:
    assert estimate_usage_cost_usd({"provider": "other", "model": "unknown"}) is None
