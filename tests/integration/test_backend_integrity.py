import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.modules.content.batch_executor import BatchExecutor, ContentBatchOperations
from juya_admin_api.modules.content.production_store import ProductionStore
from juya_admin_api.modules.content.repository import SQLAlchemyContentRepository
from juya_admin_api.modules.content.service import ContentService
from juya_admin_api.modules.media.repository import SQLAlchemyMediaAdminRepository
from juya_admin_api.modules.media.service import MediaAdminService


def sessions():
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated MySQL required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_content_effect_receipt_commits_once_and_rolls_back_on_interruption(monkeypatch):
    engine, factory = sessions()
    store = ProductionStore(factory, require_review=False)
    content = ContentService(SQLAlchemyContentRepository(factory, require_review=False))
    now = datetime.now(UTC)
    series = await store.create_series("B integrity", uuid4().hex, None)
    scene = await store.create_scene(series["id"], "dialogue")
    draft = await content.create_revision(scene, None, "b-tests", now)
    operations = ContentBatchOperations(content, store)
    try:
        first = await operations("TAGS", scene, {"tags": ["one"]}, "b-tests", "b:" + scene, now)
        second = await operations("TAGS", scene, {"tags": ["one"]}, "b-tests", "b:" + scene, now)
        assert first == second
        assert (await content.get_revision(draft.id)).version == 2
        original = ContentBatchOperations._execute

        async def interrupted(self, *args):
            await original(self, *args)
            raise asyncio.CancelledError()

        monkeypatch.setattr(ContentBatchOperations, "_execute", interrupted)
        with pytest.raises(asyncio.CancelledError):
            await operations(
                "TAGS", scene, {"tags": ["two"]}, "b-tests", "b:rollback:" + scene, now
            )
        assert (await content.get_revision(draft.id)).content["tags"] == ["one"]
        assert (await content.get_revision(draft.id)).version == 2
        async with factory() as session:
            assert (
                await session.scalar(
                    text("SELECT COUNT(*) FROM batch_operation_receipt WHERE operation_key=:key"),
                    {"key": "b:rollback:" + scene},
                )
                == 0
            )
        monkeypatch.setattr(ContentBatchOperations, "_execute", original)
        await operations("TAGS", scene, {"tags": ["two"]}, "b-tests", "b:rollback:" + scene, now)
        assert (await content.get_revision(draft.id)).version == 3
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_committed_effect_survives_batch_crash_and_old_worker_is_fenced():
    engine, factory = sessions()
    store = ProductionStore(factory, require_review=False)
    content = ContentService(SQLAlchemyContentRepository(factory, require_review=False))
    repo = SQLAlchemyMediaAdminRepository(factory)
    admin = MediaAdminService(repo)
    now = datetime.now(UTC)
    series = await store.create_series("B recovery", uuid4().hex, None)
    scene = await store.create_scene(series["id"], "dialogue")
    draft = await content.create_revision(scene, None, "b-tests", now)
    operation = ContentBatchOperations(content, store)
    batch = await admin.create_batch(
        business_key="b-crash:" + scene,
        job_type="TAGS",
        target_ids=(scene,),
        actor_id="b-tests",
        now=now,
        input_payload={"tags": ["recover"]},
    )

    async def crash(*args):
        await operation(*args)
        raise asyncio.CancelledError()

    try:
        with pytest.raises(asyncio.CancelledError):
            await BatchExecutor(admin, repo, crash).run(batch.id, now)
        assert (await content.get_revision(draft.id)).version == 2
        old_token = (await admin.get_batch(batch.id)).lease_token
        assert batch.id not in await repo.list_recoverable_batches(now)
        assert batch.id in await repo.list_recoverable_batches(now + timedelta(minutes=6))
        recovered = await BatchExecutor(admin, repo, operation).run(
            batch.id, now + timedelta(minutes=6)
        )
        assert recovered.status == "COMPLETED"
        assert recovered.success_count == 1
        assert (await content.get_revision(draft.id)).version == 2
        item = (await admin.list_batch_items(batch.id))[0]
        assert item.attempt_count == 2
        assert not await repo.finish_claimed_batch_item(
            batch.id, item.item_key, old_token, {}, "OLD_WORKER", now
        )
        assert (await admin.get_batch(batch.id)).success_count == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_batch_recovery_claims_only_one_worker():
    engine, factory = sessions()
    repo = SQLAlchemyMediaAdminRepository(factory)
    admin = MediaAdminService(repo)
    now = datetime.now(UTC)
    batch = await admin.create_batch(
        business_key="b-claims:" + uuid4().hex,
        job_type="VALIDATE",
        target_ids=("fixture",),
        actor_id="b-tests",
        now=now,
    )
    try:
        claims = await asyncio.gather(
            *(repo.claim_batch(batch.id, now, uuid4().hex[:26]) for _ in range(5))
        )
        assert claims.count(True) == 1
        claims = await asyncio.gather(
            *(
                repo.claim_batch(batch.id, now + timedelta(minutes=6), uuid4().hex[:26])
                for _ in range(5)
            )
        )
        assert claims.count(True) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cancel_recovery_reconciles_a_committed_effect_without_restarting_work():
    engine, factory = sessions()
    store = ProductionStore(factory, require_review=False)
    content = ContentService(SQLAlchemyContentRepository(factory, require_review=False))
    repo = SQLAlchemyMediaAdminRepository(factory)
    admin = MediaAdminService(repo)
    now = datetime.now(UTC)
    series = await store.create_series("B cancel recovery", uuid4().hex, None)
    scene = await store.create_scene(series["id"], "dialogue")
    draft = await content.create_revision(scene, None, "b-tests", now)
    operation = ContentBatchOperations(content, store)
    batch = await admin.create_batch(
        business_key="b-cancel:" + scene,
        job_type="TAGS",
        target_ids=(scene,),
        actor_id="b-tests",
        now=now,
        input_payload={"tags": ["committed"]},
    )

    async def crash(*args):
        await operation(*args)
        raise asyncio.CancelledError()

    try:
        with pytest.raises(asyncio.CancelledError):
            await BatchExecutor(admin, repo, crash).run(batch.id, now)
        await admin.cancel_batch(batch.id, now=now)

        async def forbidden(*args):
            pytest.fail("cancelled batch must only reconcile the existing receipt")

        result = await BatchExecutor(admin, repo, forbidden).run(
            batch.id, now + timedelta(minutes=6)
        )
        assert result.status == "CANCELLED"
        assert (result.success_count, result.failure_count) == (1, 0)
        assert (await admin.list_batch_items(batch.id))[0].status == "SUCCEEDED"
        assert (await content.get_revision(draft.id)).version == 2
    finally:
        await engine.dispose()
