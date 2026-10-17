"""Verified original tariffs, applied per attempt, without invoice or discount claims."""

from decimal import Decimal
from urllib.parse import urlsplit

from .schemas import ListPriceEstimate

SOURCES = [
    "https://help.aliyun.com/zh/model-studio/qwen-flash",
    "https://help.aliyun.com/zh/model-studio/model-pricing",
]


def provider_region(base_url: str | None) -> str | None:
    """Map the actual configured provider endpoint; unknown endpoints remain unpriced."""
    return (
        "cn-beijing"
        if isinstance(base_url, str) and urlsplit(base_url).hostname == "dashscope.aliyuncs.com"
        else None
    )


def estimate(
    *,
    model: str | None,
    region: str | None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    audio_ms: int | None = None,
) -> ListPriceEstimate:
    """Estimate one request using proven model, region and measured or labelled upper units."""
    result = ListPriceEstimate(
        sources=SOURCES,
        assumptions=[
            "官方公开原价估算,不代表供应商账单;未计优惠、免费额度、缓存折扣、切片、存储与流量",
        ],
    )
    if region != "cn-beijing":
        return result.model_copy(update={"unknown_reason": "缺少可核验的北京地域依据或地域不支持"})
    if model == "qwen-flash":
        if (
            type(input_tokens) is not int
            or type(output_tokens) is not int
            or min(input_tokens, output_tokens) < 0
        ):
            return result.model_copy(update={"unknown_reason": "缺少有效的单次输入或输出Token计量"})
        tier = next(
            (
                rates
                for limit, rates in (
                    (128000, ("0.15", "1.5")),
                    (256000, ("0.6", "6")),
                    (1000000, ("1.2", "12")),
                )
                if input_tokens <= limit
            ),
            None,
        )
        if tier is None:
            return result.model_copy(update={"unknown_reason": "输入Token超过已核验价格范围"})
        amount = (
            Decimal(input_tokens) * Decimal(tier[0]) + Decimal(output_tokens) * Decimal(tier[1])
        ) / Decimal(1000000)
    elif model == "qwen3-asr-flash-filetrans":
        if type(audio_ms) is not int or audio_ms <= 0:
            return result.model_copy(update={"unknown_reason": "缺少实际解码音频时长"})
        amount = Decimal((audio_ms + 999) // 1000) * Decimal("0.00022")
        result.assumptions.append("音频按既有预算预留规则向上取整秒,此估算不证明账单舍入规则")
    else:
        return result.model_copy(update={"unknown_reason": "模型未知或没有已核验公开原价"})
    return result.model_copy(update={"amount_cny": format(amount, "f")})
