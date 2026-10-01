from datetime import UTC, date, datetime

import pytest

from juya_admin_api.modules.analytics import events


def test_events_reject_personal_payload_and_dimensions() -> None:
    with pytest.raises(ValueError):
        events.validate_event("SCENE_STARTED", "user:123", {})
    with pytest.raises(ValueError):
        events.validate_event("CONTACT_SUBMITTED", "ALL", {"wechat_id": "private"})


def test_complete_event_metrics_are_anonymous_and_reaggregate_identically() -> None:
    day = date(2026, 9, 30)
    now = datetime(2026, 9, 30, 3, tzinfo=UTC)
    items = [
        events.AnalyticsEvent("a", "CONTACT_PROMPT_EXPOSED", now, "ALL", {}),
        events.AnalyticsEvent("b", "CONTACT_SUBMITTED", now, "ALL", {}),
        events.AnalyticsEvent("c", "LIMITED_STARTED", now, "campaign:one", {"mode": 3}),
        events.AnalyticsEvent("d", "SCENE_COMPLETED", now, "scene:one", {}),
        events.AnalyticsEvent("e", "FEEDBACK_RESPONDED", now, "ALL", {"response_seconds": 30}),
    ]
    result = events.aggregate_events(items, day)
    assert result == events.aggregate_events([*items, items[0]], day)
    assert result[("CONTACT_FUNNEL", "NUMERATOR")] == 1
    assert result[("CONTACT_FUNNEL", "DENOMINATOR")] == 1
    assert result[("LIMITED_STARTS", "MODE_3")] == 1
    assert result[("SCENE_COMPLETIONS", "scene:one")] == 1
    assert result[("FEEDBACK_RESPONSE_SECONDS", "ALL")] == 30


def test_limited_rates_follow_grant_and_start_cohorts_instead_of_event_day() -> None:
    grant_day = date(2026, 9, 28)
    events_on_later_day = [
        events.AnalyticsEvent(
            "grant", "LIMITED_GRANTED", datetime(2026, 9, 28, tzinfo=UTC), "ALL", {"mode": 3}
        ),
        events.AnalyticsEvent(
            "start",
            "LIMITED_STARTED",
            datetime(2026, 9, 29, tzinfo=UTC),
            "ALL",
            {"mode": 3, "cohort_day": "2026-09-28"},
        ),
        events.AnalyticsEvent(
            "complete",
            "LIMITED_COMPLETED",
            datetime(2026, 9, 30, tzinfo=UTC),
            "ALL",
            {
                "mode": 3,
                "cohort_day": "2026-09-28",
                "started_day": "2026-09-29",
                "before_expiry": True,
            },
        ),
    ]
    grants = events.aggregate_events(events_on_later_day, grant_day)
    assert grants[("LIMITED_STARTS", "NUMERATOR")] == 1
    assert grants[("LIMITED_STARTS", "DENOMINATOR")] == 1
    starts = events.aggregate_events(events_on_later_day, date(2026, 9, 29))
    assert starts[("LIMITED_COMPLETIONS", "NUMERATOR")] == 1
    assert starts[("LIMITED_COMPLETIONS", "DENOMINATOR")] == 1


def test_mode_and_category_dimensions_are_not_counted_twice() -> None:
    day = date(2026, 9, 30)
    values = [
        events.AnalyticsEvent(
            "m", "LIMITED_STARTED", datetime(2026, 9, 30, tzinfo=UTC), "MODE_3", {"mode": 3}
        ),
        events.AnalyticsEvent(
            "f",
            "FEEDBACK_CREATED",
            datetime(2026, 9, 30, tzinfo=UTC),
            "CONTENT",
            {"category": "CONTENT"},
        ),
    ]
    result = events.aggregate_events(values, day)
    assert result[("LIMITED_STARTS", "MODE_3")] == 1
    assert result[("FEEDBACK_NEW", "CONTENT")] == 1
