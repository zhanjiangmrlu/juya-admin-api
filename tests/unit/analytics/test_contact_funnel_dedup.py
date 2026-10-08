from datetime import UTC, date, datetime, timedelta

from juya_admin_api.modules.analytics.events import AnalyticsEvent, aggregate_events


def test_one_exposure_two_submissions_keep_one_conversion_and_change_count() -> None:
    # 功能:验证一次曝光两次提交保持一次转化并记录修改次数。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    day = date(2026, 10, 1)
    now = datetime(2026, 10, 1, tzinfo=UTC)
    cohort = "a" * 32
    events = [
        AnalyticsEvent("expose", "CONTACT_PROMPT_EXPOSED", now, "ALL", {"contact_cohort": cohort}),
        AnalyticsEvent(
            "first", "CONTACT_SUBMITTED", now, "ALL", {"prompted": True, "contact_cohort": cohort}
        ),
        AnalyticsEvent(
            "duplicate",
            "CONTACT_SUBMITTED",
            now,
            "ALL",
            {"prompted": True, "contact_cohort": cohort},
        ),
        AnalyticsEvent("change", "CONTACT_CHANGED", now, "ALL", {}),
    ]
    result = aggregate_events(events, day)
    assert result[("CONTACT_FUNNEL", "NUMERATOR")] == 1
    assert result[("CONTACT_FUNNEL", "DENOMINATOR")] == 1
    assert result[("CONTACT_SUBMISSIONS", "ALL")] == 1
    assert result[("CONTACT_CHANGES", "ALL")] == 1


def test_cross_day_submission_binds_the_exposure_cohort() -> None:
    # 功能:验证跨日提交归属原曝光群组。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    now = datetime(2026, 10, 1, tzinfo=UTC)
    cohort = "b" * 32
    items = [
        AnalyticsEvent("expose", "CONTACT_PROMPT_EXPOSED", now, "ALL", {"contact_cohort": cohort}),
        AnalyticsEvent(
            "first",
            "CONTACT_SUBMITTED",
            now + timedelta(days=1),
            "ALL",
            {"contact_cohort": cohort, "cohort_day": "2026-10-01", "prompted": True},
        ),
    ]
    result = aggregate_events(items, now.date())
    assert result[("CONTACT_FUNNEL", "NUMERATOR")] == result[("CONTACT_FUNNEL", "DENOMINATOR")] == 1


def test_legacy_two_submissions_are_deduplicated_by_internal_subject_without_exporting_it() -> None:
    # 功能:验证旧版两次提交按内部主体去重且不导出主体标识。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    now = datetime(2026, 10, 1, tzinfo=UTC)
    items = [
        AnalyticsEvent("expose", "CONTACT_PROMPT_EXPOSED", now, "ALL", {}, contact_subject=42),
        AnalyticsEvent(
            "change", "CONTACT_SUBMITTED", now + timedelta(minutes=1), "ALL", {}, contact_subject=42
        ),
        AnalyticsEvent("first", "CONTACT_SUBMITTED", now, "ALL", {}, contact_subject=42),
    ]
    result = aggregate_events(items, now.date())
    assert result[("CONTACT_FUNNEL", "NUMERATOR")] == result[("CONTACT_FUNNEL", "DENOMINATOR")] == 1
    assert result[("CONTACT_SUBMISSIONS", "ALL")] == 1
    assert all("42" not in dimension for _, dimension in result)
