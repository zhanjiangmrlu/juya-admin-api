"""Real SQL CAS coordinates module metadata with the legacy discovery version."""

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.modules.learning_features.modules import LearningModulesStore
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


@pytest.mark.asyncio
async def test_sql_module_metadata_readiness_cas_global_version_and_replay():
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated migrated MySQL required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    actor = new_ulid(datetime.now(UTC))
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            sessions = async_sessionmaker(bind=connection, join_transaction_mode="create_savepoint")
            store = LearningModulesStore(sessions, {"scene_learning": True, "short_reading": True})
            try:
                listed = (await store.listing())["items"]
                assert len(listed) == 4
                rows = {item["module_type"]: item for item in listed}
                assert rows["grammar"]["ready"] is False
                assert rows["short_reading"]["ready"] is True
                async with sessions() as session:
                    previous = await session.scalar(
                        text("SELECT version FROM discovery_config_state WHERE id=1")
                    )
                first = dict(
                    expected_version=rows["short_reading"]["config_version"],
                    display_name="短文精选",
                    sort_order=4,
                    enabled=True,
                )
                result = await store.command("short_reading", actor, "rename", first)
                assert result["entry_path"] == rows["short_reading"]["entry_path"]
                assert result["discovery_version"] == previous + 1
                assert result["config_version"] == first["expected_version"] + 1
                with pytest.raises(AppError) as stale:
                    await store.command("short_reading", actor, "stale", first)
                assert stale.value.code == "CONFIG_VERSION_CONFLICT"
                with pytest.raises(AppError) as unready:
                    await store.command(
                        "grammar",
                        actor,
                        "enable-unready",
                        dict(
                            expected_version=rows["grammar"]["config_version"],
                            display_name="Grammar",
                            sort_order=5,
                            enabled=True,
                        ),
                    )
                assert unready.value.code == "LEARNING_MODULE_NOT_READY"
                later = await store.command(
                    "short_reading",
                    actor,
                    "disable",
                    first
                    | {
                        "expected_version": result["config_version"],
                        "enabled": False,
                        "sort_order": 8,
                    },
                )
                assert await store.command("short_reading", actor, "rename", first) == result
                with pytest.raises(AppError):
                    await store.command(
                        "short_reading", actor, "rename", first | {"display_name": "other"}
                    )
                async with sessions() as session:
                    assert (
                        await session.scalar(
                            text("SELECT version FROM discovery_config_state WHERE id=1")
                        )
                        == previous + 2
                    )
                    audit = await session.scalar(
                        text(
                            "SELECT COUNT(*) FROM audit_event WHERE actor_public_id=:actor "
                            "AND action='learning_module.update'"
                        ),
                        {"actor": actor},
                    )
                    assert audit == 2
                current = {item["module_type"]: item for item in (await store.listing())["items"]}
                assert current["short_reading"]["enabled"] is False
                assert current["short_reading"]["config_version"] == later["config_version"]
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
