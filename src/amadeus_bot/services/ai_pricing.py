from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

MILLION_TOKENS = Decimal(1_000_000)


@dataclass(frozen=True)
class TokenRates:
    input_usd: Decimal
    output_usd: Decimal
    cached_input_usd: Decimal | None = None


@dataclass(frozen=True)
class ModelPricing:
    off_peak: TokenRates
    peak: TokenRates | None = None
    note: str = ""


def _rates(input_usd: str, output_usd: str, cached_input_usd: str | None = None) -> TokenRates:
    return TokenRates(
        input_usd=Decimal(input_usd),
        output_usd=Decimal(output_usd),
        cached_input_usd=Decimal(cached_input_usd) if cached_input_usd is not None else None,
    )


# Prices are USD per 1M tokens and were checked on 2026-09-04.
# DeepSeek source: https://api-docs.deepseek.com/quick_start/pricing
DEEPSEEK_PRICING = {
    "deepseek-v4-flash": ModelPricing(
        off_peak=_rates("0.22", "0.66", "0.007"),
        peak=_rates("0.44", "1.32", "0.014"),
    ),
    "deepseek-v4-pro": ModelPricing(
        off_peak=_rates("0.66", "1.98", "0.022"),
        peak=_rates("1.32", "3.96", "0.044"),
    ),
    "deepseek-v4-flash-vision-exp": ModelPricing(
        off_peak=_rates("0.22", "0.66", "0.007"),
        peak=_rates("0.44", "1.32", "0.014"),
    ),
}

# Huanyan source: https://api.huanyan.ltd/pricing
# The site lists standard prices. The configured rates below are the requested
# discounted prices after multiplying those standard prices by 0.1.
HUANYAN_PRICING = {
    "codex-auto-review": ModelPricing(_rates("0.30", "1.50"), note="桓衍打折后价格（标准价 × 0.1）"),
    "gpt-5.4": ModelPricing(_rates("0.30", "1.80"), note="桓衍打折后价格（标准价 × 0.1）"),
    "gpt-5.4-mini": ModelPricing(_rates("0.30", "1.80"), note="桓衍打折后价格（标准价 × 0.1）"),
    "gpt-5.5": ModelPricing(_rates("0.50", "3.00"), note="桓衍打折后价格（标准价 × 0.1）"),
    "gpt-5.6-luna": ModelPricing(_rates("0.10", "0.60"), note="桓衍打折后价格（标准价 × 0.1）"),
    "gpt-5.6-sol": ModelPricing(_rates("1.00", "6.00"), note="桓衍打折后价格（标准价 × 0.1）"),
    "gpt-5.6-sol-wm": ModelPricing(_rates("0.30", "1.50"), note="桓衍打折后价格（标准价 × 0.1）"),
    "gpt-5.6-terra": ModelPricing(_rates("0.25", "1.50"), note="桓衍打折后价格（标准价 × 0.1）"),
}

MODEL_PRICING = {
    **{("deepseek", model): pricing for model, pricing in DEEPSEEK_PRICING.items()},
    **{("huanyan", model): pricing for model, pricing in HUANYAN_PRICING.items()},
}

# gpt_gateway was the original provider label used for the same Huanyan endpoint.
PROVIDER_ALIASES = {"gpt_gateway": "huanyan"}


def provider_display_name(provider: str) -> str:
    if provider == "gpt_gateway":
        return "gpt_gateway（桓衍旧名）"
    return provider


def estimate_usage_cost_usd(row: Mapping[str, Any]) -> Decimal | None:
    provider = PROVIDER_ALIASES.get(str(row["provider"]), str(row["provider"]))
    pricing = MODEL_PRICING.get((provider, str(row["model"])))
    if pricing is None:
        return None

    if pricing.peak is None:
        return _cost_for_tokens(
            input_tokens=_token_count(row, "input_tokens"),
            output_tokens=_token_count(row, "output_tokens"),
            cached_tokens=_token_count(row, "cached_tokens"),
            rates=pricing.off_peak,
        )

    peak_input = _token_count(row, "peak_input_tokens")
    peak_output = _token_count(row, "peak_output_tokens")
    peak_cached = _token_count(row, "peak_cached_tokens")
    off_peak_input = max(_token_count(row, "input_tokens") - peak_input, 0)
    off_peak_output = max(_token_count(row, "output_tokens") - peak_output, 0)
    off_peak_cached = max(_token_count(row, "cached_tokens") - peak_cached, 0)
    return _cost_for_tokens(peak_input, peak_output, peak_cached, pricing.peak) + _cost_for_tokens(
        off_peak_input,
        off_peak_output,
        off_peak_cached,
        pricing.off_peak,
    )


def format_usd(cost: Decimal) -> str:
    precision = 6 if cost < Decimal("0.01") else 4
    return f"${cost:.{precision}f}"


def _cost_for_tokens(
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    rates: TokenRates,
) -> Decimal:
    cached = min(max(cached_tokens, 0), max(input_tokens, 0))
    uncached = max(input_tokens - cached, 0)
    if rates.cached_input_usd is None:
        cached = 0
        uncached = max(input_tokens, 0)
    return (
        Decimal(uncached) * rates.input_usd
        + Decimal(cached) * (rates.cached_input_usd or Decimal(0))
        + Decimal(max(output_tokens, 0)) * rates.output_usd
    ) / MILLION_TOKENS


def _token_count(row: Mapping[str, Any], key: str) -> int:
    return int(row.get(key) or 0)
