from dataclasses import dataclass

from juya_admin_api.modules.content.domain import PublishCheck, PublishCheckSummary
from juya_admin_api.shared.errors import AppError


@dataclass(frozen=True, slots=True)
class PublishFacts:
    has_title: bool
    has_entries: bool
    media_ready: bool
    has_audio: bool


def evaluate_publish_checks(facts: PublishFacts) -> tuple[PublishCheck, ...]:
    # 功能:将标题、词条、可用媒体和音频事实转换为发布检查项。
    # 参数:
    #     facts: 发布事实对象,包含是否有标题、词条、可用媒体及音频。
    # 返回:各项发布检查的代码、严重级别及是否通过。
    return (
        PublishCheck("TITLE_REQUIRED", "ERROR", facts.has_title),
        PublishCheck("ENTRY_REQUIRED", "ERROR", facts.has_entries),
        PublishCheck("MEDIA_NOT_READY", "ERROR", facts.media_ready),
        PublishCheck("AUDIO_MISSING", "ERROR", facts.has_audio),
    )


def require_publishable(
    checks: tuple[PublishCheck, ...], acknowledged_warning_codes: frozenset[str]
) -> PublishCheckSummary:
    # 功能:阻止失败检查和未确认提醒,否则返回发布检查摘要。
    # 参数:
    #     checks: 待判断的发布检查项,包含严重级别、代码及通过状态。
    #     acknowledged_warning_codes: 管理员已确认的发布提醒代码集合。
    # 返回:发布就绪状态、错误代码及提醒代码摘要。
    errors = tuple(
        sorted(check.code for check in checks if check.severity == "ERROR" and not check.passed)
    )
    warnings = tuple(
        sorted(check.code for check in checks if check.severity == "WARNING" and not check.passed)
    )
    if errors:
        raise AppError(
            "PUBLISH_CHECK_FAILED",
            "内容未通过发布检查",
            409,
            {"error_codes": list(errors)},
        )
    unacknowledged = tuple(code for code in warnings if code not in acknowledged_warning_codes)
    if unacknowledged:
        raise AppError(
            "PUBLISH_WARNING_NOT_ACKNOWLEDGED",
            "发布提醒尚未确认",
            409,
            {"warning_codes": list(unacknowledged)},
        )
    return PublishCheckSummary("", True, errors, warnings)
