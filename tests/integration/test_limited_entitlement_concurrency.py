import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import create_engine, text

from juya_admin_api.infrastructure.db.session import create_engine as create_async_engine
from juya_admin_api.infrastructure.db.session import create_session_factory
from juya_admin_api.modules.access_policy.domain import AccessDecision, AccessLevel
from juya_admin_api.modules.limited_entitlements.domain import LimitedEntitlement
from juya_admin_api.modules.limited_entitlements.repository import (
    SQLAlchemyLimitedEntitlementRepository,
)
from juya_admin_api.modules.limited_entitlements.service import LimitedEntitlementService
from juya_admin_api.shared.errors import AppError

PROJECT_ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 9, 28, 17, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_capacity_one_and_fifty_concurrent_opens_are_atomic() -> None:
    database_url = os.getenv("JUYA_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("JUYA_TEST_DATABASE_URL is required for MySQL integration tests")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    alembic_command.upgrade(config, "head")
    _seed(database_url)

    async_url = database_url.replace("mysql+pymysql", "mysql+asyncmy")
    engine = create_async_engine(async_url)
    service = LimitedEntitlementService(
        SQLAlchemyLimitedEntitlementRepository(create_session_factory(engine))
    )
    grants = await asyncio.gather(
        service.grant(
            "01J00000000000000000000400",
            "01J00000000000000000000410",
            "admin-1",
            "grant-user-1",
            NOW,
        ),
        service.grant(
            "01J00000000000000000000401",
            "01J00000000000000000000410",
            "admin-1",
            "grant-user-2",
            NOW,
        ),
        return_exceptions=True,
    )
    successes = [item for item in grants if isinstance(item, LimitedEntitlement)]
    failures = [item for item in grants if isinstance(item, AppError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].code == "CAMPAIGN_CAPACITY_REACHED"

    user_id = successes[0].user_id
    decisions = await asyncio.gather(
        *(service.activate_for_scene(user_id, "01J00000000000000000000420", NOW) for _ in range(50))
    )
    await engine.dispose()

    assert all(isinstance(item, AccessDecision) for item in decisions)
    assert all(item.level is AccessLevel.LIMITED for item in decisions)
    assert {item.activated_at for item in decisions} == {NOW}
    assert len({item.earliest_expires_at for item in decisions}) == 1

    verify_engine = create_engine(database_url)
    with verify_engine.connect() as connection:
        granted_count = connection.scalar(
            text(
                "SELECT granted_user_count FROM limited_campaign_version "
                "WHERE public_id = '01J00000000000000000000410'"
            )
        )
        activation_count = connection.scalar(
            text(
                "SELECT COUNT(DISTINCT activated_at) FROM limited_entitlement "
                "WHERE campaign_version_id = (SELECT id FROM limited_campaign_version "
                "WHERE public_id = '01J00000000000000000000410')"
            )
        )
    verify_engine.dispose()
    assert granted_count == 1
    assert activation_count == 1


def _seed(database_url: str) -> None:
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in (
            "limited_entitlement_operation",
            "limited_entitlement",
            "limited_campaign_scene",
            "limited_campaign_version",
            "limited_campaign",
            "scene_revision",
            "scene",
            "content_template",
            "content_series",
            "user_account",
        ):
            connection.execute(text(f"DELETE FROM {table}"))
        connection.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        connection.execute(
            text(
                "INSERT INTO user_account (public_id, juya_number, status) VALUES "
                "('01J00000000000000000000400', 'JUYA-400', 'ACTIVE'), "
                "('01J00000000000000000000401', 'JUYA-401', 'ACTIVE')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO content_series (public_id, slug, title, status) "
                "VALUES ('01J00000000000000000000402', 'limited', 'Limited', 'PUBLISHED')"
            )
        )
        series_id = connection.scalar(text("SELECT id FROM content_series"))
        connection.execute(
            text(
                "INSERT INTO content_template "
                "(template_type, version, required_modules, validation_rules) "
                "VALUES ('limited', 1, JSON_ARRAY(), JSON_OBJECT())"
            )
        )
        template_id = connection.scalar(text("SELECT id FROM content_template"))
        connection.execute(
            text(
                "INSERT INTO scene (public_id, series_id, template_id, title, status) "
                "VALUES ('01J00000000000000000000420', :series_id, :template_id, "
                "'Limited Scene', 'PUBLISHED')"
            ),
            {"series_id": series_id, "template_id": template_id},
        )
        scene_id = connection.scalar(text("SELECT id FROM scene"))
        connection.execute(
            text(
                "INSERT INTO scene_revision "
                "(public_id, scene_id, version_no, status, content_snapshot, created_by, "
                "published_at) VALUES ('01J00000000000000000000421', :scene_id, 1, "
                "'PUBLISHED', JSON_OBJECT(), 'admin-1', :now)"
            ),
            {"scene_id": scene_id, "now": NOW},
        )
        revision_id = connection.scalar(text("SELECT id FROM scene_revision"))
        connection.execute(
            text("UPDATE scene SET published_revision_id = :revision_id WHERE id = :scene_id"),
            {"revision_id": revision_id, "scene_id": scene_id},
        )
        connection.execute(
            text(
                "INSERT INTO limited_campaign (public_id, name, status) "
                "VALUES ('01J00000000000000000000409', 'Trial', 'OPEN')"
            )
        )
        campaign_id = connection.scalar(text("SELECT id FROM limited_campaign"))
        connection.execute(
            text(
                "INSERT INTO limited_campaign_version "
                "(public_id, campaign_id, version_no, status, duration_days, "
                "activation_window_days, capacity) VALUES "
                "('01J00000000000000000000410', :campaign_id, 1, 'OPEN', 3, 7, 1)"
            ),
            {"campaign_id": campaign_id},
        )
        version_id = connection.scalar(text("SELECT id FROM limited_campaign_version"))
        connection.execute(
            text(
                "UPDATE limited_campaign SET current_version_id = :version_id "
                "WHERE id = :campaign_id"
            ),
            {"version_id": version_id, "campaign_id": campaign_id},
        )
        connection.execute(
            text(
                "INSERT INTO limited_campaign_scene "
                "(campaign_version_id, scene_id, position, scene_revision_id) "
                "VALUES (:version_id, :scene_id, 1, :revision_id)"
            ),
            {
                "version_id": version_id,
                "scene_id": scene_id,
                "revision_id": revision_id,
            },
        )
    engine.dispose()
