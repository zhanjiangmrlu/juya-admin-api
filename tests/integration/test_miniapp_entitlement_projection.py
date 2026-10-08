import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.modules.content.repository import SQLAlchemyContentRepository
from juya_admin_api.shared.ids import new_ulid


@pytest.mark.asyncio
async def test_entitlement_projection_reads_actual_learning_days_in_isolated_mysql() -> None:
    # 功能:验证独立 MySQL 中权益投影读取真实学习天数。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated MySQL required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 10, 2, 8, tzinfo=UTC)
    user, campaign, version, gift = [new_ulid(now) for _ in range(4)]
    start = (now - timedelta(days=3)).replace(tzinfo=None)
    end = (now - timedelta(hours=1)).replace(tzinfo=None)
    try:
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO user_account(public_id,juya_number,status) "
                    "VALUES(:id,:number,'ACTIVE')"
                ),
                {"id": user, "number": "JY" + user[-12:]},
            )
            internal_user = await session.scalar(text("SELECT LAST_INSERT_ID()"))
            await session.execute(
                text(
                    "INSERT INTO limited_campaign(public_id,name,status) "
                    "VALUES(:id,'Projection','OPEN')"
                ),
                {"id": campaign},
            )
            internal_campaign = await session.scalar(text("SELECT LAST_INSERT_ID()"))
            await session.execute(
                text(
                    "INSERT INTO limited_campaign_version(public_id,campaign_id,version_no,status,"
                    "duration_days,activation_window_days,capacity) "
                    "VALUES(:id,:campaign,1,'OPEN',3,7,10)"
                ),
                {"id": version, "campaign": internal_campaign},
            )
            internal_version = await session.scalar(text("SELECT LAST_INSERT_ID()"))
            await session.execute(
                text(
                    "INSERT INTO limited_entitlement(public_id,user_id,campaign_version_id,status,"
                    "granted_at,start_deadline,activated_at,expires_at,updated_at) "
                    "VALUES(:id,:user,:version,'ACTIVE',:start,:end,:start,:end,:end)"
                ),
                {
                    "id": gift,
                    "user": internal_user,
                    "version": internal_version,
                    "start": start,
                    "end": end,
                },
            )
            for index, stamp in enumerate(
                [
                    start + timedelta(hours=1),
                    start + timedelta(hours=2),
                    start + timedelta(days=1),
                    now.replace(tzinfo=None),
                ]
            ):
                await session.execute(
                    text(
                        "INSERT INTO "
                        "analytics_event(id,event_key,user_id,event_type,occurred_at,"
                        "dimension,payload) "
                        "VALUES(:id,:key,:user,'USER_ACTIVE',:stamp,'ALL',JSON_OBJECT())"
                    ),
                    {
                        "id": new_ulid(now),
                        "key": f"projection:{user}:{index}",
                        "user": internal_user,
                        "stamp": stamp,
                    },
                )
        result = await SQLAlchemyContentRepository(sessions).entitlements(user, now)
        assert result["formal"] == []
        item = result["limited"][0]
        assert item["status"] == "ENDED"
        assert item["scene_ids"] == []
        assert item["achievements"] == {
            "completed_scenes": 0,
            "learning_days": 2,
            "favorite_vocabulary": 0,
            "favorite_phrases": 0,
        }
    finally:
        async with sessions() as session, session.begin():
            await session.execute(
                text("DELETE FROM analytics_event WHERE event_key LIKE :prefix"),
                {"prefix": f"projection:{user}:%"},
            )
            await session.execute(
                text("DELETE FROM limited_entitlement WHERE public_id=:id"), {"id": gift}
            )
            await session.execute(
                text("DELETE FROM limited_campaign_version WHERE public_id=:id"), {"id": version}
            )
            await session.execute(
                text("DELETE FROM limited_campaign WHERE public_id=:id"), {"id": campaign}
            )
            await session.execute(
                text("DELETE FROM user_account WHERE public_id=:id"), {"id": user}
            )
        await engine.dispose()
