import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from juya_admin_api.modules.content.batch_executor import BatchExecutor
from juya_admin_api.modules.media.service import InMemoryMediaAdminRepository, MediaAdminService

NOW = datetime.now(UTC)


@pytest.mark.asyncio
async def test_expired_running_batch_resumes_but_live_lease_cannot_be_stolen() -> None:
    # 功能:验证租约过期批次可恢复且有效租约不能抢占。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    repo = InMemoryMediaAdminRepository()
    admin = MediaAdminService(repo)
    started = asyncio.Event()
    calls = []

    async def operation(kind, target, payload, actor, key, now):
        # 功能:模拟可被中断的批次操作,供租约与取消恢复检查。
        # 参数:
        #     kind: 测试选择的操作或媒体类别,决定所执行的模拟分支。
        #     target: 批操作的业务目标标识,决定要修改的场景或条目。
        #     payload: 待提交的业务请求载荷;进程脚本中为标准输入文本。
        #     actor: 执行批操作的管理员标识。
        #     key: 当前业务请求的幂等键,用于重复请求对账。
        #     now: 测试指定的当前时间,用于稳定计算期限、状态迁移和事件时间。
        # 返回:本用例预设的调用结果或所构造的测试资源。
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
    # 功能:验证过期取消批次标记中断项并完成收尾。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
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
        # 功能:模拟可被中断的批次操作,供租约与取消恢复检查。
        # 参数:
        #     args: 被替换调用的位置参数;命令替身中为可执行文件及命令行参数列表。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        pytest.fail("cancelled interrupted batch must not start work")

    result = await BatchExecutor(admin, repo, operation).run(batch.id, NOW + timedelta(minutes=6))
    assert result.status == "CANCELLED"
    items = await admin.list_batch_items(batch.id)
    assert [item.status for item in items] == ["FAILED", "CANCELLED"]
    assert items[0].error_code == "BATCH_INTERRUPTED_CANCELLED"
    assert result.failure_count == 1
