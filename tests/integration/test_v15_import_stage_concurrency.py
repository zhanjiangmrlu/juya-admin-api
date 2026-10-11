import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.modules.content_import.stages import StageStore
from juya_admin_api.shared.errors import AppError


@pytest.mark.asyncio
async def test_import_stage_race_fencing_cancel_and_input_identity():
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated migrated JUYA_TEST_DATABASE_URL required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    store = StageStore(sessions)
    item = uuid4().hex
    now = datetime.now(UTC)
    ids = []

    async def ensure(hash_value="a" * 64, version="v1"):
        async with sessions() as session, session.begin():
            return await store.ensure_within(session, item, "PARSE", hash_value, version)

    try:
        ids = await asyncio.gather(*[ensure() for _ in range(6)])
        assert len(set(ids)) == 1
        stage_id = ids[0]
        queued = await asyncio.gather(*[store.enqueue(stage_id, now) for _ in range(6)])
        assert sum(queued) == 1
        async with sessions() as session:
            count = await session.scalar(
                text("SELECT COUNT(*) FROM admin_outbox WHERE aggregate_public_id=:id"),
                {"id": stage_id},
            )
            assert count == 1
        claims = await asyncio.gather(*[store.claim(stage_id, str(i), now) for i in range(6)])
        assert sum(claim is not None for claim in claims) == 1
        old = next(claim for claim in claims if claim)
        expired = now + timedelta(seconds=181)
        with pytest.raises(AppError):
            await store.finish(old, {"stale": True}, expired)
        replacement = await store.claim(stage_id, "replacement", expired)
        assert replacement and replacement.fencing_version > old.fencing_version
        with pytest.raises(AppError):
            await store.fail(old, "OLD", expired, True)
        await store.fail(replacement, "TEMPORARY", expired, True)
        assert stage_id not in await store.due(expired)
        retry_at = expired + timedelta(seconds=31)
        assert stage_id in await store.due(retry_at)
        assert await store.enqueue(stage_id, retry_at)
        active = await store.claim(stage_id, "retry", retry_at)
        assert active
        # New input invalidates running old work; no stale output can become reusable.
        changed = await ensure("b" * 64)
        ids.append(changed)
        assert changed != stage_id
        with pytest.raises(AppError):
            await store.finish(active, {"old_input": True}, retry_at)
        current = await store.claim(changed, "new", retry_at)
        assert current
        await store.finish(current, {"fresh": True}, retry_at)
        assert await ensure("b" * 64) == changed
        assert await store.claim(changed, "duplicate", retry_at) is None
        new_config = await ensure("b" * 64, "v2")
        ids.append(new_config)
        assert new_config != changed
        cancelled = await store.claim(new_config, "cancelled", retry_at)
        assert cancelled
        async with sessions() as session, session.begin():
            await store.cancel_item_within(session, item)
        with pytest.raises(AppError):
            await store.finish(cancelled, {}, retry_at)
        assert await ensure("b" * 64, "v2") == new_config
        assert await store.claim(new_config, "resurrect", retry_at) is None
        assert not await store.enqueue(new_config, retry_at)
    finally:
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "DELETE FROM admin_outbox WHERE aggregate_public_id IN "
                    "(SELECT public_id FROM content_import_stage WHERE item_id=:item)"
                ),
                {"item": item},
            )
            await session.execute(
                text("DELETE FROM content_import_stage WHERE item_id=:item"), {"item": item}
            )
        await engine.dispose()
