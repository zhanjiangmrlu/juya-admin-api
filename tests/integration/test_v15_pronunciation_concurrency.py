import asyncio
import os
import time
from uuid import uuid4

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.modules.pronunciation.domain import Config, Target, cache_key
from juya_admin_api.modules.pronunciation.models import attempts, budgets, tasks
from juya_admin_api.modules.pronunciation.repository import SqlPronunciationRepository
from juya_admin_api.shared.errors import AppError


@pytest.mark.asyncio
async def test_unique_task_and_atomic_hard_budget_and_lease_fence():
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("JUYA_TEST_DATABASE_URL required; migrate head before isolated test")
    url = url.replace("mysql+pymysql://", "mysql+asyncmy://")
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    repository = SqlPronunciationRepository(sessions)
    target = Target(uuid4().hex, uuid4().hex)
    config = Config(enabled=True, price_version="test")
    key = cache_key(target, config)
    second_target = Target(uuid4().hex, uuid4().hex)
    second_key = cache_key(second_target, config)
    period = uuid4().hex
    now = int(time.time())
    try:
        jobs = await asyncio.gather(*[repository.enqueue(target, key, config) for _ in range(8)])
        assert len({job["public_id"] for job in jobs}) == 1
        claims = await asyncio.gather(
            *[repository.claim(jobs[0]["public_id"], now) for _ in range(8)]
        )
        assert sum(claim is not None for claim in claims) == 1
        winner = next(claim for claim in claims if claim is not None)
        async with sessions() as session, session.begin():
            await session.execute(
                insert(budgets).values(
                    period=period, character_limit=32, cost_limit=32 * 30, enabled=True
                )
            )
        second = await repository.enqueue(second_target, second_key, config)
        second_claim = await repository.claim(second["public_id"], now)
        reservations = await asyncio.gather(
            repository.reserve(winner, period, config, 32, now),
            repository.reserve(second_claim, period, config, 32, now),
            return_exceptions=True,
        )
        assert sum(isinstance(result, str) for result in reservations) == 1
        assert sum(isinstance(result, AppError) for result in reservations) == 1
        with pytest.raises(AppError, match="预算"):
            await repository.reserve(winner, period, config, 1, now)
        with pytest.raises(AppError, match="租约"):
            await repository.fenced_update(winner | {"lease_token": "wrong"}, status="READY")
        async with sessions() as session:
            row = (
                (await session.execute(select(budgets).where(budgets.c.period == period)))
                .mappings()
                .one()
            )
            assert row["reserved_characters"] == 32
            assert row["reserved_cost"] == 960
        # Expired dispatched job goes UNKNOWN rather than making another charge.
        assert await repository.claim(winner["public_id"], now + 181) is None
        assert (await repository.task(winner["public_id"]))["status"] == "UNKNOWN"
    finally:
        async with sessions() as session, session.begin():
            await session.execute(attempts.delete().where(attempts.c.period == period))
            await session.execute(budgets.delete().where(budgets.c.period == period))
            await session.execute(tasks.delete().where(tasks.c.cache_key.in_([key, second_key])))
        await engine.dispose()
