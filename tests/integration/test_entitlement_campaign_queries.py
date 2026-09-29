import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from juya_admin_api.infrastructure.db.session import create_engine as async_engine
from juya_admin_api.infrastructure.db.session import create_session_factory
from juya_admin_api.modules.campaigns.repository import SQLAlchemyCampaignRepository
from juya_admin_api.modules.formal_entitlements.repository import (
    SQLAlchemyEntitlementQueryRepository,
)
from juya_admin_api.modules.limited_entitlements.repository import (
    SQLAlchemyLimitedEntitlementRepository,
)
from juya_admin_api.shared.errors import AppError

ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 9, 29, 6, tzinfo=UTC)
USER = "01J00000000000000000000600"
USER_2 = "01J00000000000000000000609"
PACKAGE = "01J00000000000000000000601"
FORMAL = "01J00000000000000000000602"
CAMPAIGN = "01J00000000000000000000603"
VERSION = "01J00000000000000000000604"
LIMITED = "01J00000000000000000000605"


@pytest.fixture
def database_url() -> str:
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("JUYA_TEST_DATABASE_URL is required for MySQL integration tests")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in (
            "limited_campaign_operation",
            "limited_entitlement_operation",
            "formal_entitlement_operation",
            "limited_entitlement",
            "formal_entitlement",
            "limited_campaign_scene",
            "limited_campaign_version",
            "limited_campaign",
            "content_package_scene",
            "content_package",
            "user_account",
        ):
            conn.execute(text(f"DELETE FROM {table}"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        conn.execute(
            text(
                "INSERT INTO user_account (public_id, juya_number, status) "
                "VALUES (:id, 'JUYA-600', 'ACTIVE'), "
                "(:second, 'JUYA-609', 'ACTIVE')"
            ),
            {"id": USER, "second": USER_2},
        )
        conn.execute(
            text(
                "INSERT INTO content_package (public_id, name, status, sort_order) "
                "VALUES (:id, 'Starter', 'ACTIVE', 1)"
            ),
            {"id": PACKAGE},
        )
        conn.execute(
            text(
                "INSERT INTO formal_entitlement (public_id, user_id, package_id, "
                "term, status, granted_at, expires_at, version, updated_at) "
                "VALUES (:id, (SELECT id FROM user_account WHERE public_id = :user), "
                "(SELECT id FROM content_package WHERE public_id = :package), "
                "'MONTH_1', 'ACTIVE', :now, :expires, 1, :now)"
            ),
            {
                "id": FORMAL,
                "user": USER,
                "package": PACKAGE,
                "now": NOW,
                "expires": datetime(2026, 10, 29, 6, tzinfo=UTC),
            },
        )
        conn.execute(
            text(
                "INSERT INTO limited_campaign (public_id, name, status) "
                "VALUES (:id, 'Launch', 'DRAFT')"
            ),
            {"id": CAMPAIGN},
        )
        conn.execute(
            text(
                "INSERT INTO limited_campaign_version (public_id, campaign_id, "
                "version_no, status, duration_days, activation_window_days, "
                "capacity, granted_user_count) VALUES (:id, "
                "(SELECT id FROM limited_campaign WHERE "
                "public_id = :campaign), 1, 'DRAFT', 3, 7, 1, 1)"
            ),
            {"id": VERSION, "campaign": CAMPAIGN},
        )
        conn.execute(
            text(
                "UPDATE limited_campaign SET current_version_id = "
                "(SELECT id FROM limited_campaign_version WHERE public_id = :version) "
                "WHERE public_id = :campaign"
            ),
            {"version": VERSION, "campaign": CAMPAIGN},
        )
        conn.execute(
            text(
                "INSERT INTO limited_entitlement (public_id, user_id, "
                "campaign_version_id, status, granted_at, start_deadline, "
                "updated_at) VALUES (:id, (SELECT id FROM user_account WHERE "
                "public_id = :user), (SELECT id FROM limited_campaign_version "
                "WHERE public_id = :version), 'PENDING', :now, :deadline, :now)"
            ),
            {
                "id": LIMITED,
                "user": USER,
                "version": VERSION,
                "now": NOW,
                "deadline": datetime(2026, 10, 6, 6, tzinfo=UTC),
            },
        )
    engine.dispose()
    return url


@pytest.mark.asyncio
async def test_unified_page_filters_and_detail_views(database_url: str) -> None:
    engine = async_engine(database_url.replace("mysql+pymysql", "mysql+asyncmy"))
    repo = SQLAlchemyEntitlementQueryRepository(create_session_factory(engine))
    try:
        page = await repo.list_entitlements({"user_id": USER}, 1, 1)
        assert (page["page"], page["page_size"], page["total"]) == (1, 1, 2)
        assert page["items"][0]["id"] == LIMITED
        assert page["items"][0]["type"] == "LIMITED"
        formal = await repo.list_entitlements({"type": "FORMAL"}, 1, 20)
        assert formal["total"] == 1
        assert formal["items"][0]["id"] == FORMAL
        formal_detail = await repo.get_formal(FORMAL)
        assert formal_detail["package_name"] == "Starter"
        assert formal_detail["granted_at"].tzinfo == UTC
        assert formal_detail["available_operations"] == ["RENEW", "PAUSE", "REVOKE"]
        limited_detail = await repo.get_limited(LIMITED)
        assert limited_detail["campaign_name"] == "Launch"
        assert limited_detail["start_deadline"].tzinfo == UTC
        assert limited_detail["available_operations"] == ["EXTEND_START_DEADLINE", "REVOKE"]
        limited_repo = SQLAlchemyLimitedEntitlementRepository(create_session_factory(engine))
        assert (await limited_repo.get_limited(LIMITED))["campaign_name"] == "Launch"
        assert [item["id"] for item in await repo.list_packages()] == [PACKAGE]
        with pytest.raises(AppError):
            await repo.list_entitlements({"status": "ACTIVE' OR 1=1 --"}, 1, 20)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_capacity_change_racing_grant_never_oversells(database_url: str) -> None:
    engine = async_engine(database_url.replace("mysql+pymysql", "mysql+asyncmy"))
    factory = create_session_factory(engine)
    campaigns = SQLAlchemyCampaignRepository(factory)
    entitlements = SQLAlchemyLimitedEntitlementRepository(factory)
    try:
        await campaigns.command(CAMPAIGN, "capacity", expected_version=1, capacity=2, now=NOW)
        await campaigns.command(CAMPAIGN, "open", expected_version=2, now=NOW)
        results = await asyncio.gather(
            entitlements.grant(USER_2, VERSION, "admin", "grant-second", NOW),
            campaigns.command(CAMPAIGN, "capacity", expected_version=3, capacity=1, now=NOW),
            return_exceptions=True,
        )
        assert len([item for item in results if isinstance(item, AppError)]) == 1
        detail = await campaigns.get(CAMPAIGN)
        assert (
            detail["current_version"]["granted_user_count"] <= detail["current_version"]["capacity"]
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_copied_locked_version_keeps_immutable_fields(database_url: str) -> None:
    engine = async_engine(database_url.replace("mysql+pymysql", "mysql+asyncmy"))
    repo = SQLAlchemyCampaignRepository(create_session_factory(engine))
    try:
        # A successful grant marks the version immutable in the existing grant path.
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session, session.begin():
            await session.execute(
                text("UPDATE limited_campaign_version SET locked_at = :now WHERE public_id = :id"),
                {"now": NOW, "id": VERSION},
            )
        copied = await repo.command(CAMPAIGN, "copy", expected_version=1, now=NOW)
        assert copied["version"] == 2
        assert copied["current_version"]["id"] != VERSION
        assert copied["current_version"]["duration_days"] == 3
        with pytest.raises(AppError) as locked:
            await repo.save(CAMPAIGN, expected_version=2, name="Launch", duration_days=5, now=NOW)
        assert locked.value.code == "CAMPAIGN_VERSION_LOCKED"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_campaign_optimistic_version_and_atomic_capacity(database_url: str) -> None:
    engine = async_engine(database_url.replace("mysql+pymysql", "mysql+asyncmy"))
    repo = SQLAlchemyCampaignRepository(create_session_factory(engine))
    try:
        page = await repo.list({}, 1, 20)
        assert page["total"] == 1
        assert page["items"][0]["id"] == CAMPAIGN
        detail = await repo.get(CAMPAIGN)
        assert detail["version"] == 1
        assert detail["created_at"].tzinfo == UTC
        assert detail["current_version"]["id"] == VERSION
        updated = await repo.save(CAMPAIGN, expected_version=1, name="Launch 2", now=NOW)
        assert updated["version"] == 2
        with pytest.raises(AppError) as stale:
            await repo.save(CAMPAIGN, expected_version=1, name="Stale", now=NOW)
        assert stale.value.status_code == 409
        results = await asyncio.gather(
            repo.command(CAMPAIGN, "capacity", expected_version=2, capacity=2, now=NOW),
            repo.command(CAMPAIGN, "capacity", expected_version=2, capacity=0, now=NOW),
            return_exceptions=True,
        )
        assert len([item for item in results if isinstance(item, dict)]) == 1
        assert len([item for item in results if isinstance(item, AppError)]) == 1
        current = await repo.get(CAMPAIGN)
        assert current["current_version"]["capacity"] in {1, 2}
        assert (
            current["current_version"]["capacity"]
            >= current["current_version"]["granted_user_count"]
        )
    finally:
        await engine.dispose()
