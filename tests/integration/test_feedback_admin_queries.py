import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from juya_admin_api.infrastructure.db.session import create_engine, create_session_factory
from juya_admin_api.modules.feedback.repository import SQLAlchemyFeedbackRepository
from juya_admin_api.modules.feedback.service import FeedbackService
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 29, 1, 0, tzinfo=UTC)


def _database_url() -> str:
    # 功能:读取独立测试数据库 URL,未配置时跳过集成用例。
    # 参数:无。
    # 返回:字符串。
    url = os.environ.get("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("JUYA_TEST_DATABASE_URL is required for MySQL integration tests")
    return url


def _upgrade_schema(database_url: str) -> None:
    # 功能:将独立测试数据库迁移到当前 Alembic 版本。
    # 参数:
    #     database_url: 独立测试数据库的连接 URL,由测试环境提供。
    # 返回:无;完成模拟状态更新、调用记录或检查。
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(config, "head")


@pytest.mark.asyncio
async def test_admin_list_filters_and_aggregate_detail_keep_internal_notes_private() -> None:
    # 功能:验证管理列表筛选及聚合详情不会泄露内部备注。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    sync_url = _database_url()
    _upgrade_schema(sync_url)
    engine = create_engine(sync_url.replace("mysql+pymysql://", "mysql+asyncmy://", 1))
    factory = create_session_factory(engine)
    repository = SQLAlchemyFeedbackRepository(factory)
    service = FeedbackService(repository)
    user_ids = [new_ulid(NOW), new_ulid(NOW + timedelta(seconds=1))]
    ticket_ids: list[str] = []
    admin_public_id = new_ulid(NOW)
    admin_name = "feedback-test-admin"
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO admin_user(public_id,username,password_hash,"
                    "totp_secret_ciphertext) "
                    "VALUES (:id,:name,'unused',X'00')"
                ),
                {"id": admin_public_id, "name": admin_name},
            )
            admin_id = str(await session.scalar(text("SELECT LAST_INSERT_ID()")))
            for index, user_id in enumerate(user_ids, start=1):
                await session.execute(
                    text(
                        "INSERT INTO user_account (public_id, juya_number, status) "
                        "VALUES (:public_id, :juya_number, 'ACTIVE')"
                    ),
                    {"public_id": user_id, "juya_number": f"JYFEEDBACK{index:03d}"},
                )
                internal_user_id = await session.scalar(text("SELECT LAST_INSERT_ID()"))
                await session.execute(
                    text("INSERT INTO user_profile (user_id, source) VALUES (:user_id, 'WECHAT')"),
                    {"user_id": internal_user_id},
                )

        overdue = await service.create(
            user_ids[0], "CONTENT", "字幕错误", {"page": "scene"}, [], "create-overdue", NOW
        )
        active = await service.create(
            user_ids[1],
            "FUNCTION",
            "按钮不可用",
            {"page": "practice"},
            ["feedback/private/shot.png"],
            "create-active",
            NOW + timedelta(minutes=1),
        )
        ticket_ids.extend([overdue.id, active.id])
        await service.start_processing(active.id, admin_id, "start-1", NOW + timedelta(hours=1))
        await service.request_supplement(
            active.id,
            "请补充复现步骤",
            admin_public_id,
            "supplement-1",
            NOW + timedelta(hours=10),
        )

        paused_page = await service.list_admin(
            {"keyword": user_ids[1], "sla": "PAUSED"}, 1, 20, NOW + timedelta(hours=20)
        )
        assert [item.id for item in paused_page.items] == [active.id]
        assert paused_page.items[0].sla_state == "PAUSED"

        first_note = await service.add_internal_note(
            active.id,
            "admin-1",
            "仅管理员可见的排查记录",
            "note-1",
            NOW + timedelta(hours=11),
        )
        replayed_note = await service.add_internal_note(
            active.id,
            "admin-1",
            "重复请求不得覆盖原内容",
            "note-1",
            NOW + timedelta(hours=12),
        )
        assert replayed_note == first_note

        await service.supply(
            active.id,
            "补充后的复现步骤",
            user_ids[1],
            "supply-1",
            NOW + timedelta(hours=20),
        )
        await service.start_processing(
            active.id, "admin-1", "start-2", NOW + timedelta(hours=20, minutes=1)
        )
        await service.resolve(
            active.id,
            "RESOLVED",
            "已修复按钮状态",
            "admin-1",
            "resolve-1",
            NOW + timedelta(hours=21),
        )

        overdue_page = await service.list_admin(
            {
                "status": "PENDING",
                "category": "CONTENT",
                "keyword": overdue.id,
                "sla": "OVERDUE",
            },
            1,
            20,
            NOW + timedelta(hours=49),
        )
        assert overdue_page.total == 1
        assert [item.id for item in overdue_page.items] == [overdue.id]

        detail = await service.get_admin(active.id)
        first_admin_event = next(event for event in detail.timeline if event.actor_id == admin_id)
        assert first_admin_event.actor_name == admin_name
        assert (
            next(event for event in detail.timeline if event.actor_id == admin_public_id).actor_name
            == admin_name
        )
        assert all(
            event.actor_name is None for event in detail.timeline if event.actor_type == "USER"
        )
        assert [event.occurred_at for event in detail.timeline] == sorted(
            event.occurred_at for event in detail.timeline
        )
        assert [note.content for note in detail.internal_notes] == ["仅管理员可见的排查记录"]
        assert detail.rounds[0].request_text == "请补充复现步骤"
        assert detail.rounds[0].supplement_text == "补充后的复现步骤"
        assert detail.replies[0].template == "RESOLVED"
        assert detail.replies[0].note == "已修复按钮状态"
        assert detail.screenshots[0].object_key == "feedback/private/shot.png"
        assert not hasattr(await service.get(active.id), "internal_notes")

        with pytest.raises(AppError) as invalid_filter:
            await service.list_admin({"unknown": "value"}, 1, 20, NOW)
        assert invalid_filter.value.code == "FEEDBACK_FILTER_INVALID"
    finally:
        async with factory() as session, session.begin():
            for ticket_id in ticket_ids:
                await session.execute(
                    text("DELETE FROM admin_outbox WHERE aggregate_public_id = :ticket_id"),
                    {"ticket_id": ticket_id},
                )
                await session.execute(
                    text("DELETE FROM feedback_ticket WHERE public_id = :ticket_id"),
                    {"ticket_id": ticket_id},
                )
            for user_id in user_ids:
                await session.execute(
                    text("DELETE FROM user_account WHERE public_id = :user_id"),
                    {"user_id": user_id},
                )
            await session.execute(
                text("DELETE FROM admin_user WHERE public_id=:id"), {"id": admin_public_id}
            )
        await engine.dispose()
