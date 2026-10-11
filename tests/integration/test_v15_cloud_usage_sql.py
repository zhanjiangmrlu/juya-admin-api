import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.modules.cloud_usage.models import attempts, budgets
from juya_admin_api.modules.cloud_usage.store import CloudUsageStore
from juya_admin_api.modules.content_import.stages import StageStore
from juya_admin_api.shared.errors import AppError


@pytest.mark.asyncio
async def test_daily_hard_budget_concurrent_reserve_unknown_once_and_lease_fence():
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated migrated MySQL required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    stages = StageStore(sessions)
    usage = CloudUsageStore()
    kind = "TEST_" + uuid4().hex[:20]
    stage_ids = []
    try:
        for _ in range(2):
            async with sessions() as session, session.begin():
                stage_ids.append(
                    await stages.ensure_within(
                        session, uuid4().hex, "AI_LEXICON", uuid4().hex, "test"
                    )
                )
        claims = [await stages.claim(stage, "worker", datetime.now(UTC)) for stage in stage_ids]

        async def reserve(claim):
            async with sessions() as session, session.begin():
                return await usage.reserve_within(session, claim, kind, 100, 100)

        results = await asyncio.gather(
            *[reserve(claim) for claim in claims], return_exceptions=True
        )
        diagnostics = [
            (
                type(result).__name__,
                result.code
                if isinstance(result, AppError)
                else getattr(getattr(result, "orig", None), "args", [None])[0]
                if isinstance(result, Exception)
                else "reserved",
            )
            for result in results
        ]
        assert sum(isinstance(result, str) for result in results) == 1, diagnostics
        assert sum(isinstance(result, AppError) for result in results) == 1, diagnostics
        index = next(index for index, result in enumerate(results) if isinstance(result, str))
        winner, attempt_id = claims[index], results[index]
        async with sessions() as session, session.begin():
            await usage.record_within(session, winner, attempt_id, "UNKNOWN")
        with pytest.raises(AppError, match="重复提交"):
            await reserve(winner)
        async with sessions() as session:
            budget = (
                (await session.execute(select(budgets).where(budgets.c.kind == kind)))
                .mappings()
                .one()
            )
            assert budget["reserved_units"] == 100
            attempt = (
                (await session.execute(select(attempts).where(attempts.c.public_id == attempt_id)))
                .mappings()
                .one()
            )
            assert attempt["status"] == "UNKNOWN"
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE content_import_stage SET fencing_version=fencing_version+1 "
                    "WHERE public_id=:id"
                ),
                {"id": winner.id},
            )
        with pytest.raises(AppError, match="租约"):
            async with sessions() as session, session.begin():
                await usage.record_within(session, winner, attempt_id, "SUCCEEDED", actual_units=5)
    finally:
        async with sessions() as session, session.begin():
            await session.execute(attempts.delete().where(attempts.c.kind == kind))
            await session.execute(budgets.delete().where(budgets.c.kind == kind))
            for stage_id in stage_ids:
                await session.execute(
                    text("DELETE FROM content_import_stage WHERE public_id=:id"), {"id": stage_id}
                )
        await engine.dispose()
