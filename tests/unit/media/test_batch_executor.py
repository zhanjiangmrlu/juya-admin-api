import asyncio
from datetime import UTC, datetime

import pytest

from juya_admin_api.modules.media.service import InMemoryMediaAdminRepository, MediaAdminService
from juya_admin_api.shared.errors import AppError

NOW = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_batch_executes_once_and_keeps_success_when_another_item_fails() -> None:
    from juya_admin_api.modules.content.batch_executor import BatchExecutor

    repository = InMemoryMediaAdminRepository()
    admin = MediaAdminService(repository)
    calls: list[str] = []

    async def operate(
        kind: str, target: str, payload: dict[str, object], actor: str, key: str, now: datetime
    ) -> dict[str, object]:
        calls.append(target)
        await asyncio.sleep(0.001)
        if target == "broken":
            raise AppError("REVISION_VERSION_CONFLICT", "stale", 409)
        return {"scene_id": target, "version": 2}

    batch = await admin.create_batch(
        business_key="batch-1",
        job_type="TAGS",
        target_ids=("good", "broken"),
        actor_id="admin",
        now=NOW,
        input_payload={"tags": ["travel"]},
    )
    worker = BatchExecutor(admin, repository, operate)
    await asyncio.gather(worker.run(batch.id, NOW), worker.run(batch.id, NOW))
    await worker.run(batch.id, NOW)
    assert sorted(calls) == ["broken", "good"]
    result = await admin.get_batch(batch.id)
    assert (result.success_count, result.failure_count, result.status) == (
        1,
        1,
        "COMPLETED_WITH_ERRORS",
    )


@pytest.mark.asyncio
async def test_cancel_skips_only_unstarted_items_and_running_item_finishes() -> None:
    from juya_admin_api.modules.content.batch_executor import BatchExecutor

    repository = InMemoryMediaAdminRepository()
    admin = MediaAdminService(repository)
    started = asyncio.Event()
    finish = asyncio.Event()
    calls: list[str] = []

    async def operate(
        kind: str, target: str, payload: dict[str, object], actor: str, key: str, now: datetime
    ) -> dict[str, object]:
        calls.append(target)
        started.set()
        await finish.wait()
        return {"version": 2}

    batch = await admin.create_batch(
        business_key="cancel-1",
        job_type="VALIDATE",
        target_ids=("running", "unstarted"),
        actor_id="admin",
        now=NOW,
    )
    future = asyncio.create_task(BatchExecutor(admin, repository, operate).run(batch.id, NOW))
    await started.wait()
    await admin.cancel_batch(batch.id, now=NOW)
    finish.set()
    await future
    assert calls == ["running"]
    items = await admin.list_batch_items(batch.id)
    assert [item.status for item in items] == ["SUCCEEDED", "CANCELLED"]
