"""Official original prices are estimates, never invoice amounts or unknown zeros."""

import importlib.util
from decimal import Decimal

import pytest


def estimator():
    module = "juya_admin_api.modules.content_production.list_prices"
    assert importlib.util.find_spec(module), "verified per-attempt list price estimator missing"
    from juya_admin_api.modules.content_production.list_prices import estimate

    return estimate


@pytest.mark.parametrize(
    ("tokens", "input_rate", "output_rate"),
    [
        (128000, "0.15", "1.5"),
        (128001, "0.6", "6"),
        (256000, "0.6", "6"),
        (256001, "1.2", "12"),
        (1000000, "1.2", "12"),
    ],
)
def test_single_request_tier_uses_official_decimal_rates(tokens, input_rate, output_rate):
    result = estimator()(
        model="qwen-flash", region="cn-beijing", input_tokens=tokens, output_tokens=1000
    )
    assert Decimal(result.amount_cny) == (
        Decimal(tokens) * Decimal(input_rate) + Decimal(1000) * Decimal(output_rate)
    ) / Decimal(1000000)
    assert result.basis == "official_list_price"
    assert result.price_version == "aliyun-list-cn-beijing-2026-10-14"


@pytest.mark.parametrize(
    "values",
    [
        {"model": None},
        {"region": None},
        {"region": "ap-southeast-1"},
        {"model": "other"},
        {"input_tokens": None},
        {"input_tokens": "invalid"},
        {"output_tokens": True},
        {"output_tokens": None},
        {"input_tokens": 1000001},
    ],
)
def test_unknown_model_region_units_and_unsupported_tier_are_not_zero(values):
    args = dict(model="qwen-flash", region="cn-beijing", input_tokens=100, output_tokens=10)
    result = estimator()(**(args | values))
    assert result.amount_cny is None and result.unknown_reason


def test_asr_uses_decoded_duration_rounded_up_per_existing_reservation():
    result = estimator()(model="qwen3-asr-flash-filetrans", region="cn-beijing", audio_ms=1001)
    assert Decimal(result.amount_cny) == Decimal("0.00044")
    result = estimator()(model="qwen3-asr-flash-filetrans", region="cn-beijing", audio_ms=None)
    assert result.amount_cny is None


def test_planned_ai_is_labelled_upper_bound_and_only_supported_provider_is_priced():
    from types import SimpleNamespace

    from juya_admin_api.modules.content_production import usage

    assert callable(getattr(usage, "project_plan", None)), (
        "read-only current candidate estimate missing"
    )
    payload = {"kind": "AI_LEXICON", "sources": {"sentence": "Hello."}}
    provider = SimpleNamespace(
        model="qwen-flash", base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    result = usage.project_plan("candidate", "one", payload, provider, 4096)
    from juya_admin_api.integrations.content_ai.aliyun import input_token_upper_bound

    assert result.input_tokens_upper_bound == input_token_upper_bound(payload["sources"])
    assert result.output_tokens_upper_bound == 4096
    assert result.list_price_estimate.amount_cny is not None
    assert any("上界" in text for text in result.list_price_estimate.assumptions)
    provider.base_url = "https://unknown.example/api"
    assert (
        usage.project_plan(
            "candidate", "one", payload, provider, 4096
        ).list_price_estimate.amount_cny
        is None
    )
