import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from juya_admin_api.infrastructure.db.session import create_engine, create_session_factory
from juya_admin_api.modules.user_projection.deletion_service import (
    DeletionCleanupService,
    SQLAlchemyDeletionRepository,
)
from juya_admin_api.shared.ids import new_ulid

NOW = datetime(2026, 9, 29, 1, 0, tzinfo=UTC)


def _database_url() -> str:
    # 功能:读取独立测试数据库 URL,未配置时跳过集成用例。
    # 参数:无。
    # 返回:字符串。
    url = os.environ.get("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("JUYA_TEST_DATABASE_URL is required for MySQL integration tests")
    return url.replace("mysql+pymysql://", "mysql+asyncmy://", 1)


@pytest.mark.asyncio
async def test_sql_deletion_cleanup_anonymizes_and_replays_safely() -> None:
    # 功能:验证 SQL 注销清理匿名化数据且可安全重放。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    engine = create_engine(_database_url())
    factory = create_session_factory(engine)
    user_public_id = new_ulid(NOW)
    ticket_public_id = new_ulid(NOW)
    audit_public_id = new_ulid(NOW)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO user_account (public_id, juya_number, status, created_at) "
                    "VALUES (:public_id, :juya_number, 'DELETING', :now)"
                ),
                {
                    "public_id": user_public_id,
                    "juya_number": f"JY{user_public_id[-12:]}",
                    "now": NOW,
                },
            )
            user_id = await session.scalar(text("SELECT LAST_INSERT_ID()"))
            await session.execute(
                text(
                    "INSERT INTO feedback_ticket "
                    "(public_id, user_id, category, description, source, status, sla_hours, "
                    "supplement_rounds, reopen_count, create_idempotency_key, created_at, "
                    "updated_at) VALUES (:public_id, :user_id, 'FUNCTION', 'x', JSON_OBJECT(), "
                    "'PENDING', 48, 0, 0, 'create-delete-test', :now, :now)"
                ),
                {"public_id": ticket_public_id, "user_id": user_id, "now": NOW},
            )
            ticket_id = await session.scalar(text("SELECT LAST_INSERT_ID()"))
            await session.execute(
                text(
                    "INSERT INTO feedback_screenshot "
                    "(ticket_id, object_key, security_status) "
                    "VALUES (:ticket_id, 'feedback/delete-test.png', 'PASSED')"
                ),
                {"ticket_id": ticket_id},
            )
            await session.execute(
                text(
                    "INSERT INTO user_admin_projection "
                    "(user_id, account_status, formal_entitlement_count, "
                    "limited_entitlement_count, open_feedback_count, projection_version, "
                    "updated_at) VALUES (:user_id, 'DELETING', 0, 0, 1, 1, :now)"
                ),
                {"user_id": user_id, "now": NOW},
            )
            await session.execute(
                text(
                    "INSERT INTO audit_event "
                    "(public_id, action, object_type, object_public_id, request_id, created_at) "
                    "VALUES (:public_id, 'USER_VIEW', 'USER', :user_public_id, "
                    "'delete-test', :now)"
                ),
                {
                    "public_id": audit_public_id,
                    "user_public_id": user_public_id,
                    "now": NOW,
                },
            )

        service = DeletionCleanupService(SQLAlchemyDeletionRepository(factory))
        first = await service.cleanup("delete-event-1", user_public_id, NOW)
        replay = await service.cleanup("delete-event-1", user_public_id, NOW)

        assert first == replay
        async with factory() as session:
            feedback_user_id = await session.scalar(
                text("SELECT user_id FROM feedback_ticket WHERE public_id = :id"),
                {"id": ticket_public_id},
            )
            delete_after = await session.scalar(
                text(
                    "SELECT fs.delete_after FROM feedback_screenshot fs "
                    "JOIN feedback_ticket ft ON ft.id = fs.ticket_id "
                    "WHERE ft.public_id = :id"
                ),
                {"id": ticket_public_id},
            )
            projection_count = await session.scalar(
                text("SELECT COUNT(*) FROM user_admin_projection WHERE user_id = :id"),
                {"id": user_id},
            )
            audit_subject = await session.scalar(
                text("SELECT object_public_id FROM audit_event WHERE public_id = :id"),
                {"id": audit_public_id},
            )
        assert feedback_user_id is None
        assert delete_after is not None
        assert projection_count == 0
        assert audit_subject.startswith("deleted:")
    finally:
        async with factory() as session, session.begin():
            await session.execute(
                text("DELETE FROM deletion_cleanup_event WHERE event_id = 'delete-event-1'")
            )
            await session.execute(
                text(
                    "DELETE fs FROM feedback_screenshot fs JOIN feedback_ticket ft "
                    "ON ft.id = fs.ticket_id WHERE ft.public_id = :ticket_id"
                ),
                {"ticket_id": ticket_public_id},
            )
            await session.execute(
                text("DELETE FROM feedback_ticket WHERE public_id = :ticket_id"),
                {"ticket_id": ticket_public_id},
            )
            await session.execute(
                text("DELETE FROM audit_event WHERE public_id = :audit_id"),
                {"audit_id": audit_public_id},
            )
            await session.execute(
                text("DELETE FROM user_account WHERE public_id = :user_id"),
                {"user_id": user_public_id},
            )
        await engine.dispose()
