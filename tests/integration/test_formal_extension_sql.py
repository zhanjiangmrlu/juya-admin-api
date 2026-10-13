import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.modules.formal_entitlements.domain import (
    EntitlementOperation,
    EntitlementTerm,
    FormalEntitlementCommand,
)
from juya_admin_api.modules.formal_entitlements.repository import (
    SQLAlchemyFormalEntitlementRepository,
)
from juya_admin_api.modules.formal_entitlements.service import FormalEntitlementService
from juya_admin_api.shared.ids import new_ulid


@pytest.mark.asyncio
async def test_extension_persists_distinct_audit_preserves_pause_and_replays_once():
    database_url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("isolated MySQL required")
    engine = create_async_engine(database_url.replace("mysql+pymysql", "mysql+asyncmy"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 10, 13, tzinfo=UTC)
    user, package = new_ulid(now), new_ulid(now)
    actor = "extension-" + uuid4().hex
    service = FormalEntitlementService(SQLAlchemyFormalEntitlementRepository(sessions))
    try:
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO user_account(public_id,juya_number,status) "
                    "VALUES(:id,:number,'ACTIVE')"
                ),
                {"id": user, "number": "JUYA-" + uuid4().hex[:12]},
            )
            await session.execute(
                text(
                    "INSERT INTO content_package(public_id,name,status,sort_order) "
                    "VALUES(:id,'Extension fixture','ACTIVE',1)"
                ),
                {"id": package},
            )
        original = await service.apply_operation(
            FormalEntitlementCommand(
                user_id=user,
                package_id=package,
                operation=EntitlementOperation.GRANT,
                term=EntitlementTerm.MONTH_1,
                reason="初始授权",
            ),
            actor,
            "grant",
            now,
        )
        paused = await service.apply_operation(
            FormalEntitlementCommand(
                user_id=user,
                package_id=package,
                operation=EntitlementOperation.PAUSE,
                term=EntitlementTerm.MONTH_1,
                reason="暂停夹具",
            ),
            actor,
            "pause",
            now,
        )
        command = FormalEntitlementCommand(
            user_id=user,
            package_id=package,
            operation=EntitlementOperation.EXTEND,
            term=EntitlementTerm.MONTH_1,
            reason="补偿服务中断",
            expected_version=paused.version,
        )
        results = await asyncio.gather(
            *(service.apply_operation(command, actor, "extend-once", now) for _ in range(5))
        )
        assert all(
            result.status == "PAUSED" and result.granted_at == original.granted_at
            for result in results
        )
        assert all(
            result.expires_at == datetime(2026, 12, 13, tzinfo=UTC)
            and result.version == paused.version + 1
            for result in results
        )
        async with sessions() as session:
            operations = (
                await session.execute(
                    text(
                        "SELECT operation_type,reason FROM formal_entitlement_operation "
                        "WHERE operator_id=:actor AND idempotency_key='extend-once'"
                    ),
                    {"actor": actor},
                )
            ).all()
            assert [(row.operation_type, row.reason) for row in operations] == [
                ("EXTEND", "补偿服务中断")
            ]
    finally:
        async with sessions() as session, session.begin():
            await session.execute(
                text("DELETE FROM formal_entitlement_operation WHERE operator_id=:actor"),
                {"actor": actor},
            )
            await session.execute(
                text(
                    "DELETE FROM formal_entitlement WHERE user_id="
                    "(SELECT id FROM user_account WHERE public_id=:id)"
                ),
                {"id": user},
            )
            await session.execute(
                text("DELETE FROM user_account WHERE public_id=:id"), {"id": user}
            )
            await session.execute(
                text("DELETE FROM content_package WHERE public_id=:id"), {"id": package}
            )
        await engine.dispose()
