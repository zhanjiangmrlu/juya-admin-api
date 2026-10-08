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
from juya_admin_api.modules.formal_entitlements.domain import (
    EntitlementOperation,
    EntitlementTerm,
    FormalEntitlementCommand,
)
from juya_admin_api.modules.formal_entitlements.repository import (
    SQLAlchemyFormalEntitlementRepository,
)
from juya_admin_api.modules.formal_entitlements.service import FormalEntitlementService

PROJECT_ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 9, 28, 15, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_concurrent_same_key_extends_only_once() -> None:
    # 功能:验证同一键的并发请求只延长一次期限。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    database_url = os.getenv("JUYA_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("JUYA_TEST_DATABASE_URL is required for MySQL integration tests")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    alembic_command.upgrade(config, "head")

    sync_engine = create_engine(database_url)
    with sync_engine.begin() as connection:
        connection.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in (
            "formal_entitlement_operation",
            "formal_entitlement",
            "content_package_scene",
            "content_package",
            "user_account",
        ):
            connection.execute(text(f"DELETE FROM {table}"))
        connection.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        connection.execute(
            text(
                "INSERT INTO user_account (public_id, juya_number, status) "
                "VALUES ('01J00000000000000000000300', 'JUYA-300', 'ACTIVE')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO content_package (public_id, name, status, sort_order) "
                "VALUES ('01J00000000000000000000301', 'Package', 'ACTIVE', 1)"
            )
        )
    sync_engine.dispose()

    async_url = database_url.replace("mysql+pymysql", "mysql+asyncmy")
    engine = create_async_engine(async_url)
    repository = SQLAlchemyFormalEntitlementRepository(create_session_factory(engine))
    service = FormalEntitlementService(repository)
    grant = FormalEntitlementCommand(
        user_id="01J00000000000000000000300",
        package_id="01J00000000000000000000301",
        operation=EntitlementOperation.GRANT,
        term=EntitlementTerm.MONTH_1,
        reason="initial",
    )
    await service.apply_operation(grant, "admin-1", "grant-initial", NOW)
    renew = FormalEntitlementCommand(
        user_id=grant.user_id,
        package_id=grant.package_id,
        operation=EntitlementOperation.RENEW,
        term=EntitlementTerm.MONTH_1,
        reason="renew",
    )

    results = await asyncio.gather(
        *(service.apply_operation(renew, "admin-1", "renew-once", NOW) for _ in range(10))
    )
    await engine.dispose()

    assert len({result.expires_at for result in results}) == 1
    assert results[0].expires_at == datetime(2026, 11, 28, 15, 0, tzinfo=UTC)
    verify_engine = create_engine(database_url)
    with verify_engine.connect() as connection:
        count = connection.scalar(
            text(
                "SELECT COUNT(*) FROM formal_entitlement_operation "
                "WHERE operator_id = 'admin-1' AND idempotency_key = 'renew-once'"
            )
        )
        version = connection.scalar(text("SELECT version FROM formal_entitlement"))
    verify_engine.dispose()
    assert count == 1
    assert version == 2
