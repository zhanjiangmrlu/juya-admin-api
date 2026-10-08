import asyncio
import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from juya_admin_api.infrastructure.db.session import create_engine, create_session_factory
from juya_admin_api.modules.feedback.repository import SQLAlchemyFeedbackRepository
from juya_admin_api.modules.feedback.service import FeedbackService
from juya_admin_api.shared.ids import new_ulid

NOW = datetime(2026, 9, 28, 23, 30, tzinfo=UTC)


def _database_url() -> str:
    # 功能:读取独立测试数据库 URL,未配置时跳过集成用例。
    # 参数:无。
    # 返回:字符串。
    url = os.environ.get("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("JUYA_TEST_DATABASE_URL is required for MySQL integration tests")
    return url.replace("mysql+pymysql://", "mysql+asyncmy://", 1)


@pytest.mark.asyncio
async def test_retried_supplement_command_creates_one_timeline_and_outbox_event() -> None:
    # 功能:验证补充资料命令重试只创建一次时间线和发件箱事件。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    engine = create_engine(_database_url())
    factory = create_session_factory(engine)
    public_id = new_ulid(NOW)
    ticket_id = ""
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO user_account (public_id, juya_number, status) "
                    "VALUES (:public_id, :juya_number, 'ACTIVE')"
                ),
                {"public_id": public_id, "juya_number": f"JY{public_id[-12:]}"},
            )
            user_id = await session.scalar(text("SELECT LAST_INSERT_ID()"))
            await session.execute(
                text("INSERT INTO user_profile (user_id, source) VALUES (:user_id, 'WECHAT')"),
                {"user_id": user_id},
            )

        service = FeedbackService(SQLAlchemyFeedbackRepository(factory))
        ticket = await service.create(public_id, "FUNCTION", "problem", {}, [], "create-1", NOW)
        ticket_id = ticket.id
        await service.start_processing(ticket.id, "admin-1", "start-1", NOW)
        results = await asyncio.gather(
            *(
                service.request_supplement(
                    ticket.id, "请补充复现步骤", "admin-1", "need-more-1", NOW
                )
                for _ in range(20)
            )
        )
        assert {item.status for item in results} == {"NEED_MORE"}
        assert {item.supplement_rounds for item in results} == {1}

        async with factory() as session:
            timeline_count = await session.scalar(
                text(
                    "SELECT COUNT(*) FROM feedback_timeline t "
                    "JOIN feedback_ticket f ON f.id = t.ticket_id "
                    "WHERE f.public_id = :ticket_id AND t.event_type = 'NEED_MORE'"
                ),
                {"ticket_id": ticket.id},
            )
            command_count = await session.scalar(
                text(
                    "SELECT COUNT(*) FROM feedback_command c "
                    "JOIN feedback_ticket f ON f.id = c.ticket_id "
                    "WHERE f.public_id = :ticket_id AND c.command = 'REQUEST_SUPPLEMENT'"
                ),
                {"ticket_id": ticket.id},
            )
            outbox_count = await session.scalar(
                text(
                    "SELECT COUNT(*) FROM admin_outbox "
                    "WHERE aggregate_public_id = :ticket_id AND event_type = 'MINIAPP_MESSAGE'"
                ),
                {"ticket_id": ticket.id},
            )
            event_types = (
                (
                    await session.execute(
                        text("SELECT event_type FROM analytics_event WHERE user_id=:user_id"),
                        {"user_id": user_id},
                    )
                )
                .scalars()
                .all()
            )
        assert timeline_count == 1
        assert command_count == 1
        assert outbox_count == 1
        assert event_types.count("FEEDBACK_CREATED") == 1
        assert event_types.count("FEEDBACK_RESPONDED") == 1
        assert event_types.count("FEEDBACK_STATUS_CHANGED") == 2
    finally:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "DELETE e FROM analytics_event e JOIN user_account u ON u.id=e.user_id "
                    "WHERE u.public_id=:public_id"
                ),
                {"public_id": public_id},
            )
            await session.execute(
                text("DELETE FROM admin_outbox WHERE aggregate_public_id = :ticket_id"),
                {"ticket_id": ticket_id},
            )
            await session.execute(
                text(
                    "DELETE f FROM feedback_ticket f "
                    "JOIN user_account u ON u.id = f.user_id "
                    "WHERE u.public_id = :public_id"
                ),
                {"public_id": public_id},
            )
            await session.execute(
                text("DELETE FROM user_account WHERE public_id = :public_id"),
                {"public_id": public_id},
            )
        await engine.dispose()
