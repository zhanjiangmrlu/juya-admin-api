import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.infrastructure.tasks import maintenance
from juya_admin_api.infrastructure.tasks.maintenance import (
    _aggregate_daily,
    _cleanup_feedback_screenshots,
    _dispatch_outbox,
    _refresh_time_sensitive_projections,
    _verify_daily_integrity,
)
from juya_admin_api.shared.ids import new_ulid

TEST_DATABASE_URL = os.getenv("JUYA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="JUYA_TEST_DATABASE_URL must point to an isolated MySQL 8.4 database",
)


@pytest.mark.asyncio
async def test_projection_analytics_and_integrity_jobs_execute_on_mysql() -> None:
    assert TEST_DATABASE_URL is not None
    settings = Settings(
        database_url=TEST_DATABASE_URL.replace("mysql+pymysql://", "mysql+asyncmy://", 1)
    )

    refresh = await _refresh_time_sensitive_projections(settings)
    analytics = await _aggregate_daily(settings)
    integrity = await _verify_daily_integrity(settings)

    assert set(refresh) == {"expired_pending", "expired_active"}
    assert analytics["metric_count"] == 4
    assert set(integrity) == {
        "expired_active_entitlements",
        "broken_audio_references",
        "healthy",
    }


@pytest.mark.asyncio
async def test_outbox_delivery_and_screenshot_cleanup_are_retry_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert TEST_DATABASE_URL is not None
    now = datetime.now(UTC)
    ids = [new_ulid(now + timedelta(microseconds=offset)) for offset in range(6)]
    success_event, failed_event, success_ticket, failed_ticket, _, _ = ids
    sync_engine = create_engine(TEST_DATABASE_URL)
    with sync_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO admin_outbox "
                "(event_id, event_type, aggregate_public_id, payload, status, "
                "next_attempt_at, created_at) VALUES "
                "(:success, 'MINIAPP_MESSAGE', 'feedback-success', "
                "JSON_OBJECT('result', 'ok'), 'PENDING', :due, :now), "
                "(:failed, 'MINIAPP_MESSAGE', 'feedback-failed', "
                "JSON_OBJECT('result', 'fail'), 'PROCESSING', :due, :now)"
            ),
            {
                "success": success_event,
                "failed": failed_event,
                "due": now - timedelta(minutes=10),
                "now": now,
            },
        )
        for ticket_id, object_key in (
            (success_ticket, f"feedback/{success_ticket}/success.png"),
            (failed_ticket, f"feedback/{failed_ticket}/fail.png"),
        ):
            connection.execute(
                text(
                    "INSERT INTO feedback_ticket "
                    "(public_id, user_id, category, description, source, status, sla_hours, "
                    "create_idempotency_key, created_at, updated_at, resolved_at, closed_at) "
                    "VALUES (:public_id, NULL, 'FUNCTION', 'cleanup test', JSON_OBJECT(), "
                    "'RESOLVED', 48, :key, :now, :now, :now, :now)"
                ),
                {"public_id": ticket_id, "key": f"cleanup-{ticket_id}", "now": now},
            )
            ticket_pk = connection.scalar(text("SELECT LAST_INSERT_ID()"))
            connection.execute(
                text(
                    "INSERT INTO feedback_screenshot "
                    "(ticket_id, object_key, security_status, delete_after) "
                    "VALUES (:ticket_id, :object_key, 'PASSED', :due)"
                ),
                {
                    "ticket_id": ticket_pk,
                    "object_key": object_key,
                    "due": now - timedelta(days=1),
                },
            )

    class FakeMiniappClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def create_message(self, payload: dict[str, object], _event_id: str) -> None:
            if payload["result"] == "fail":
                raise RuntimeError("temporary upstream failure")

        async def aclose(self) -> None:
            pass

    class FakeOssProvider:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def delete_object(self, object_key: str) -> None:
            if object_key.endswith("fail.png"):
                raise RuntimeError("temporary OSS failure")

    monkeypatch.setattr(maintenance, "MiniappApiClient", FakeMiniappClient)
    monkeypatch.setattr(maintenance, "AliyunOssProvider", FakeOssProvider)
    settings = Settings(
        database_url=TEST_DATABASE_URL.replace("mysql+pymysql://", "mysql+asyncmy://", 1),
        internal_hmac_secret="integration-secret",
        oss_region="cn-hangzhou",
        oss_bucket="integration-private-bucket",
    )

    try:
        outbox = await _dispatch_outbox(settings)
        cleanup = await _cleanup_feedback_screenshots(settings)

        assert outbox == {"delivered": 1, "failed": 1}
        assert cleanup == {"deleted": 1, "failed": 1}
        with sync_engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT event_id, status, attempt_count FROM admin_outbox "
                    "WHERE event_id IN (:success, :failed)"
                ),
                {"success": success_event, "failed": failed_event},
            ).mappings()
            statuses = {row["event_id"]: (row["status"], row["attempt_count"]) for row in rows}
            assert statuses[success_event] == ("PUBLISHED", 1)
            assert statuses[failed_event] == ("PENDING", 1)
            deleted = connection.scalar(
                text(
                    "SELECT COUNT(*) FROM feedback_screenshot s "
                    "JOIN feedback_ticket t ON t.id = s.ticket_id "
                    "WHERE t.public_id IN (:success, :failed) AND s.deleted_at IS NOT NULL"
                ),
                {"success": success_ticket, "failed": failed_ticket},
            )
            assert deleted == 1
    finally:
        with sync_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM admin_outbox WHERE event_id IN (:success, :failed)"),
                {"success": success_event, "failed": failed_event},
            )
            connection.execute(
                text("DELETE FROM feedback_ticket WHERE public_id IN (:success, :failed)"),
                {"success": success_ticket, "failed": failed_ticket},
            )
        sync_engine.dispose()
