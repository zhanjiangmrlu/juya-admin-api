import os
from datetime import timedelta

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from juya_admin_api.infrastructure.db.session import create_engine as create_async_engine
from juya_admin_api.infrastructure.db.session import create_session_factory
from juya_admin_api.modules.limited_entitlements.repository import (
    SQLAlchemyLimitedEntitlementRepository,
)
from juya_admin_api.modules.limited_entitlements.service import LimitedEntitlementService
from juya_admin_api.shared.errors import AppError
from tests.integration.test_limited_entitlement_concurrency import NOW, PROJECT_ROOT, _seed


@pytest.mark.asyncio
async def test_sql_preview_is_read_only_rejects_stale_snapshot_and_replays_original_grant() -> None:
    database_url = os.environ["JUYA_TEST_DATABASE_URL"]
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    _seed(database_url)
    engine = create_async_engine(database_url.replace("mysql+pymysql", "mysql+asyncmy"))
    service = LimitedEntitlementService(
        SQLAlchemyLimitedEntitlementRepository(create_session_factory(engine))
    )
    user_id, version_id = "01J00000000000000000000400", "01J00000000000000000000410"
    sync = create_engine(database_url)
    try:
        preview = await service.preview_grant(user_id, version_id, NOW)
        assert preview["campaign_name"] == "Trial"
        assert preview["before"] is None
        assert preview["after"]["start_deadline"] == NOW + timedelta(days=7)
        with sync.connect() as connection:
            assert connection.scalar(text("SELECT COUNT(*) FROM limited_entitlement")) == 0
            assert (
                connection.scalar(text("SELECT granted_user_count FROM limited_campaign_version"))
                == 0
            )
        with sync.begin() as connection:
            connection.execute(
                text("UPDATE limited_campaign_version SET activation_window_days = 8")
            )
        with pytest.raises(AppError) as stale:
            await service.grant(
                user_id,
                version_id,
                "7",
                "preview-grant",
                NOW,
                expected_fingerprint=preview["expected_fingerprint"],
            )
        assert stale.value.code == "CAMPAIGN_PREVIEW_CONFLICT"
        fresh = await service.preview_grant(user_id, version_id, NOW)
        first = await service.grant(
            user_id,
            version_id,
            "7",
            "preview-grant",
            NOW + timedelta(seconds=30),
            expected_fingerprint=fresh["expected_fingerprint"],
            reason="运营试学",
        )
        assert first.granted_at == NOW + timedelta(seconds=30)
        assert first.start_deadline == first.granted_at + timedelta(days=8)
        again = await service.grant(
            user_id,
            version_id,
            "7",
            "preview-grant",
            NOW + timedelta(hours=1),
            expected_fingerprint=fresh["expected_fingerprint"],
            reason="运营试学",
        )
        assert again == first
        with sync.connect() as connection:
            assert connection.scalar(text("SELECT COUNT(*) FROM limited_entitlement")) == 1
            assert (
                connection.scalar(text("SELECT granted_user_count FROM limited_campaign_version"))
                == 1
            )
            assert (
                connection.scalar(text("SELECT COUNT(*) FROM limited_entitlement_operation")) == 1
            )
            assert (
                connection.scalar(text("SELECT locked_at FROM limited_campaign_version"))
                is not None
            )
            assert (
                connection.scalar(text("SELECT reason FROM limited_entitlement_operation"))
                == "运营试学"
            )
        with pytest.raises(AppError) as duplicate:
            await service.preview_grant(user_id, version_id, NOW)
        assert duplicate.value.code == "LIMITED_ENTITLEMENT_EXISTS"
    finally:
        sync.dispose()
        await engine.dispose()
