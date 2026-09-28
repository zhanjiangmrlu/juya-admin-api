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
    return (
        PublishCheck("TITLE_REQUIRED", "ERROR", facts.has_title),
        PublishCheck("ENTRY_REQUIRED", "ERROR", facts.has_entries),
        PublishCheck("MEDIA_NOT_READY", "ERROR", facts.media_ready),
        PublishCheck("AUDIO_MISSING", "WARNING", facts.has_audio),
    )


def require_publishable(
    checks: tuple[PublishCheck, ...], acknowledged_warning_codes: frozenset[str]
) -> PublishCheckSummary:
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
