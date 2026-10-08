import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from juya_admin_api.infrastructure.db.session import create_engine, create_session_factory
from juya_admin_api.modules.user_projection.repository import SQLAlchemyUserProjectionRepository
from juya_admin_api.shared.ids import new_ulid


@pytest.mark.asyncio
async def test_user_audit_resolves_internal_and_public_admin_ids_without_losing_history() -> None:
    # 验证实际 MySQL 审计查询解析两种管理员标识并保留系统及未知身份
    url = os.environ.get("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("JUYA_TEST_DATABASE_URL is required for MySQL integration tests")
    root = Path(__file__).parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(config, "head")
    engine = create_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://", 1))
    factory = create_session_factory(engine)
    admin_public_id, user_id = new_ulid(datetime.now(UTC)), new_ulid(datetime.now(UTC))
    username = f"audit-{admin_public_id}"
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO admin_user(public_id,username,password_hash,"
                    "totp_secret_ciphertext) "
                    "VALUES (:id,:name,'unused',X'00')"
                ),
                {"id": admin_public_id, "name": username},
            )
            admin_id = str(await session.scalar(text("SELECT LAST_INSERT_ID()")))
            actors = [
                admin_id,
                admin_public_id,
                "system",
                "999999999999",
                None,
                f"{admin_id}invalid",
            ]
            for index, actor in enumerate(actors):
                await session.execute(
                    text(
                        "INSERT INTO audit_event(public_id,actor_public_id,action,object_type,"
                        "object_public_id,request_id) "
                        "VALUES (:id,:actor,:action,'user',:user,:request)"
                    ),
                    {
                        "id": new_ulid(datetime.now(UTC)),
                        "actor": actor,
                        "action": f"test.{index}",
                        "user": user_id,
                        "request": new_ulid(datetime.now(UTC)),
                    },
                )
        records = await SQLAlchemyUserProjectionRepository(factory).records(user_id)
        events = {event["action"]: event for event in records["audit"]}
        assert events["test.0"]["actor_name"] == username
        assert events["test.1"]["actor_name"] == username
        assert events["test.0"]["actor_public_id"] == admin_id
        assert events["test.1"]["actor_public_id"] == admin_public_id
        assert len(events) == 6
        for index in range(2, 6):
            assert events[f"test.{index}"]["actor_name"] is None
    finally:
        async with factory() as session, session.begin():
            await session.execute(
                text("DELETE FROM audit_event WHERE object_public_id=:id"), {"id": user_id}
            )
            await session.execute(
                text("DELETE FROM admin_user WHERE public_id=:id"), {"id": admin_public_id}
            )
        await engine.dispose()
