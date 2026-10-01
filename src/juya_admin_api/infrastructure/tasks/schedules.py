from typing import Any

from juya_admin_api.infrastructure.tasks.celery_app import celery_app
from juya_admin_api.infrastructure.tasks.maintenance import (
    run_aggregate_daily,
    run_cleanup_expired_drafts,
    run_cleanup_feedback_screenshots,
    run_dispatch_outbox,
    run_refresh_time_sensitive_projections,
    run_verify_daily_integrity,
)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="juya.content.lifecycle.refresh_time_sensitive_projections"
)
def refresh_time_sensitive_projections() -> dict[str, Any]:
    return run_refresh_time_sensitive_projections()


@celery_app.task(name="juya.domain.messages.dispatch_outbox")  # type: ignore[untyped-decorator]
def dispatch_outbox() -> dict[str, Any]:
    return run_dispatch_outbox()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="juya.content.assets.cleanup_feedback_screenshots"
)
def cleanup_feedback_screenshots() -> dict[str, Any]:
    return run_cleanup_feedback_screenshots()


@celery_app.task(name="juya.content.assets.cleanup_expired_drafts")  # type: ignore[untyped-decorator]
def cleanup_expired_drafts() -> dict[str, Any]:
    return run_cleanup_expired_drafts()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="juya.content.analytics.aggregate_daily"
)
def aggregate_daily() -> dict[str, Any]:
    return run_aggregate_daily()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="juya.content.lifecycle.verify_daily_integrity"
)
def verify_daily_integrity() -> dict[str, Any]:
    return run_verify_daily_integrity()
