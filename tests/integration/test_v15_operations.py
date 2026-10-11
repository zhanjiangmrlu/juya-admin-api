"""MySQL acceptance for nullable capacity and replay-first entitlement commands."""

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text

from juya_admin_api.infrastructure.db.session import create_engine as async_engine
from juya_admin_api.infrastructure.db.session import create_session_factory
from juya_admin_api.modules.campaigns.repository import SQLAlchemyCampaignRepository
from juya_admin_api.modules.campaigns.service import CampaignService
from juya_admin_api.modules.formal_entitlements.domain import (
    EntitlementOperation,
    EntitlementTerm,
    FormalEntitlementCommand,
)
from juya_admin_api.modules.formal_entitlements.repository import (
    SQLAlchemyFormalEntitlementRepository,
)
from juya_admin_api.modules.formal_entitlements.service import FormalEntitlementService
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid

NOW = datetime(2026, 10, 11, tzinfo=UTC)


def database_url() -> str:
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if url is None:
        pytest.skip(
            "JUYA_TEST_DATABASE_URL is required; run migrations in the isolated test runner"
        )
    return url


@pytest.mark.asyncio
async def test_mysql_unlimited_capacity_missing_update_and_explicit_null():
    engine = async_engine(database_url().replace("mysql+pymysql://", "mysql+asyncmy://", 1))
    service = CampaignService(SQLAlchemyCampaignRepository(create_session_factory(engine)))
    try:
        actor = new_ulid()
        created = await service.save(
            None,
            actor_id=actor,
            idempotency_key="new",
            now=NOW,
            name="V15 unlimited",
            duration_days=3,
        )
        assert created["current_version"]["capacity"] is None
        assert created["current_version"]["activation_window_days"] == 3
        limited = await service.save(
            created["id"],
            actor_id=actor,
            idempotency_key="limit",
            now=NOW,
            expected_version=1,
            name="V15 unlimited",
            capacity=10,
        )
        preserved = await service.save(
            created["id"],
            actor_id=actor,
            idempotency_key="rename",
            now=NOW,
            expected_version=limited["version"],
            name="V15 renamed",
        )
        assert preserved["current_version"]["capacity"] == 10
        unlimited = await service.save(
            created["id"],
            actor_id=actor,
            idempotency_key="unlimit",
            now=NOW,
            expected_version=preserved["version"],
            name="V15 renamed",
            capacity=None,
            capacity_set=True,
        )
        assert unlimited["current_version"]["capacity"] is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_mysql_success_replay_returns_original_after_later_mutation():
    url = database_url()
    user, package, actor = new_ulid(), new_ulid(), new_ulid()
    sync = create_engine(url)
    with sync.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO user_account(public_id,juya_number,status) "
                "VALUES(:id,:number,'ACTIVE')"
            ),
            {"id": user, "number": f"V15-{user[-10:]}"},
        )
        conn.execute(
            text(
                "INSERT INTO content_package(public_id,name,status,sort_order) "
                "VALUES(:id,'V15 replay','ACTIVE',1)"
            ),
            {"id": package},
        )
    sync.dispose()
    engine = async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://", 1))
    service = FormalEntitlementService(
        SQLAlchemyFormalEntitlementRepository(create_session_factory(engine))
    )
    try:
        grant = FormalEntitlementCommand(
            user, package, EntitlementOperation.GRANT, EntitlementTerm.MONTH_1, expected_version=0
        )
        original = await service.apply_operation(grant, actor, "grant", NOW)
        renew = FormalEntitlementCommand(
            user, package, EntitlementOperation.RENEW, EntitlementTerm.MONTH_1, expected_version=1
        )
        await service.apply_operation(renew, actor, "renew", NOW)
        assert await service.apply_operation(grant, actor, "grant", NOW) == original
        with pytest.raises(AppError) as caught:
            await service.apply_operation(grant, actor, "new-stale-key", NOW)
        assert caught.value.code == "ENTITLEMENT_VERSION_CONFLICT"
    finally:
        await engine.dispose()
