import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from juya_admin_api.modules.content.batch_executor import BatchExecutor
from juya_admin_api.modules.media.service import InMemoryMediaAdminRepository, MediaAdminService

NOW = datetime.now(UTC)


@pytest.mark.asyncio
async def test_expired_running_batch_resumes_but_live_lease_cannot_be_stolen() -> None:
    repo = InMemoryMediaAdminRepository()
    admin = MediaAdminService(repo)
    started = asyncio.Event()
    calls = []

    async def operation(kind, target, payload, actor, key, now):
        calls.append(target)
        if len(calls) == 1:
            started.set()
            await asyncio.Event().wait()
        return {"version": 2}

    batch = await admin.create_batch(
        business_key="recover",
        job_type="TAGS",
        target_ids=("one", "two"),
        actor_id="admin",
        now=NOW,
    )
    worker = BatchExecutor(admin, repo, operation)
    task = asyncio.create_task(worker.run(batch.id, NOW))
    await started.wait()
    await worker.run(batch.id, NOW + timedelta(seconds=20))
    assert calls == ["one"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    recovered = await worker.run(batch.id, datetime.now(UTC) + timedelta(minutes=6))
    assert recovered.status == "COMPLETED"
    assert calls == ["one", "one", "two"]
    assert recovered.success_count == 2


@pytest.mark.asyncio
async def test_expired_cancelled_batch_marks_interrupted_items_and_finishes() -> None:
    repo = InMemoryMediaAdminRepository()
    admin = MediaAdminService(repo)
    batch = await admin.create_batch(
        business_key="cancel-recover",
        job_type="VALIDATE",
        target_ids=("one", "two"),
        actor_id="admin",
        now=NOW,
    )
    await repo.claim_batch(batch.id, NOW)
    await repo.claim_batch_item(batch.id, "0:one", NOW)
    await admin.cancel_batch(batch.id, now=NOW)

    async def operation(*args):
        pytest.fail("cancelled interrupted batch must not start work")

    result = await BatchExecutor(admin, repo, operation).run(batch.id, NOW + timedelta(minutes=6))
    assert result.status == "CANCELLED"
    items = await admin.list_batch_items(batch.id)
    assert [item.status for item in items] == ["FAILED", "CANCELLED"]
    assert items[0].error_code == "BATCH_INTERRUPTED_CANCELLED"
    assert result.failure_count == 1
