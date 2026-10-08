import asyncio
from datetime import UTC, datetime

import pytest

from juya_admin_api.modules.media.service import InMemoryMediaAdminRepository, MediaAdminService
from juya_admin_api.shared.errors import AppError

NOW = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_batch_executes_once_and_keeps_success_when_another_item_fails() -> None:
    # 功能:验证批任务只执行一次且单项失败不抹去其他成功结果。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    from juya_admin_api.modules.content.batch_executor import BatchExecutor

    repository = InMemoryMediaAdminRepository()
    admin = MediaAdminService(repository)
    calls: list[str] = []

    async def operate(
        kind: str, target: str, payload: dict[str, object], actor: str, key: str, now: datetime
    ) -> dict[str, object]:
        # 功能:模拟批次业务操作,记录目标并按用例阻塞、成功或失败。
        # 参数:
        #     kind: 测试选择的操作或媒体类别,决定所执行的模拟分支。
        #     target: 批操作的业务目标标识,决定要修改的场景或条目。
        #     payload: 待提交的业务请求载荷;进程脚本中为标准输入文本。
        #     actor: 执行批操作的管理员标识。
        #     key: 当前业务请求的幂等键,用于重复请求对账。
        #     now: 测试指定的当前时间,用于稳定计算期限、状态迁移和事件时间。
        # 返回:dict[str, object],由本用例预设的数据或所组装的测试资源构成。
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
    # 功能:验证取消仅跳过未开始项且运行中条目可完成。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    from juya_admin_api.modules.content.batch_executor import BatchExecutor

    repository = InMemoryMediaAdminRepository()
    admin = MediaAdminService(repository)
    started = asyncio.Event()
    finish = asyncio.Event()
    calls: list[str] = []

    async def operate(
        kind: str, target: str, payload: dict[str, object], actor: str, key: str, now: datetime
    ) -> dict[str, object]:
        # 功能:模拟批次业务操作,记录目标并按用例阻塞、成功或失败。
        # 参数:
        #     kind: 测试选择的操作或媒体类别,决定所执行的模拟分支。
        #     target: 批操作的业务目标标识,决定要修改的场景或条目。
        #     payload: 待提交的业务请求载荷;进程脚本中为标准输入文本。
        #     actor: 执行批操作的管理员标识。
        #     key: 当前业务请求的幂等键,用于重复请求对账。
        #     now: 测试指定的当前时间,用于稳定计算期限、状态迁移和事件时间。
        # 返回:dict[str, object],由本用例预设的数据或所组装的测试资源构成。
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
