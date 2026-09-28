import pytest

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.infrastructure.tasks import schedules
from juya_admin_api.infrastructure.tasks.celery_app import create_celery_app
from juya_admin_api.infrastructure.tasks.schedules import (
    aggregate_daily,
    cleanup_feedback_screenshots,
    dispatch_outbox,
    refresh_time_sensitive_projections,
    verify_daily_integrity,
)


def test_celery_routes_and_schedules_cover_content_and_domain_workers() -> None:
    app = create_celery_app(Settings(redis_url="redis://localhost:6379/15"))

    queues = {queue.name for queue in app.conf.task_queues}
    assert {
        "content.ocr",
        "content.audio",
        "content.assets",
        "content.publish",
        "content.lifecycle",
        "content.analytics",
        "domain.messages",
    } <= queues
    assert set(app.conf.beat_schedule) == {
        "refresh-time-sensitive-projections",
        "dispatch-domain-outbox",
        "cleanup-feedback-screenshots",
        "aggregate-daily-analytics",
        "verify-daily-integrity",
    }
    assert app.conf.timezone == "Asia/Shanghai"
    assert app.conf.beat_schedule["refresh-time-sensitive-projections"]["schedule"].minute == set(
        range(60)
    )
    assert app.conf.beat_schedule["dispatch-domain-outbox"]["schedule"].minute == set(
        range(0, 60, 5)
    )
    assert app.conf.beat_schedule["cleanup-feedback-screenshots"]["schedule"].minute == {0}
    assert app.conf.beat_schedule["aggregate-daily-analytics"]["schedule"].hour == {1}
    assert app.conf.beat_schedule["verify-daily-integrity"]["schedule"].hour == {2}


def test_periodic_task_entrypoints_are_serializable_and_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        schedules,
        "run_refresh_time_sensitive_projections",
        lambda: {"operation": "refresh_time_sensitive_projections"},
    )
    monkeypatch.setattr(schedules, "run_dispatch_outbox", lambda: {"operation": "dispatch_outbox"})
    monkeypatch.setattr(
        schedules,
        "run_cleanup_feedback_screenshots",
        lambda: {"operation": "cleanup_feedback_screenshots"},
    )
    monkeypatch.setattr(schedules, "run_aggregate_daily", lambda: {"operation": "aggregate_daily"})
    monkeypatch.setattr(
        schedules,
        "run_verify_daily_integrity",
        lambda: {"operation": "verify_daily_integrity"},
    )
    operations = {
        refresh_time_sensitive_projections()["operation"],
        dispatch_outbox()["operation"],
        cleanup_feedback_screenshots()["operation"],
        aggregate_daily()["operation"],
        verify_daily_integrity()["operation"],
    }

    assert operations == {
        "refresh_time_sensitive_projections",
        "dispatch_outbox",
        "cleanup_feedback_screenshots",
        "aggregate_daily",
        "verify_daily_integrity",
    }
