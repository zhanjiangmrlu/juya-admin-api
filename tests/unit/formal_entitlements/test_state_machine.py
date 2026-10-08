from datetime import UTC, datetime, timedelta

import pytest

from juya_admin_api.modules.formal_entitlements.domain import (
    EntitlementOperation,
    EntitlementTerm,
    FormalEntitlement,
    FormalEntitlementCommand,
)
from juya_admin_api.modules.formal_entitlements.repository import (
    InMemoryFormalEntitlementRepository,
)
from juya_admin_api.modules.formal_entitlements.service import FormalEntitlementService
from juya_admin_api.shared.errors import AppError

NOW = datetime(2026, 9, 28, 14, 0, tzinfo=UTC)


def command(
    operation: EntitlementOperation,
    term: EntitlementTerm | None = None,
) -> FormalEntitlementCommand:
    # 功能:构造正式权益命令,指定操作、期限及测试原因。
    # 参数:
    #     operation: 待执行的业务命令名称,如开通、暂停、恢复或撤销。
    #     term: 正式权益期限类型,例如永久或按月期限。
    # 返回:FormalEntitlementCommand,由本用例预设的数据或所组装的测试资源构成。
    return FormalEntitlementCommand(
        user_id="user-1",
        package_id="package-1",
        operation=operation,
        term=term,
        reason="test",
    )


@pytest.mark.asyncio
async def test_unexpired_renewal_uses_old_expiry_but_expired_regrant_uses_now() -> None:
    # 功能:验证未到期续期从旧到期日起算、已到期重授从当前日起算。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    repository = InMemoryFormalEntitlementRepository()
    repository.entitlements[("user-1", "package-1")] = FormalEntitlement(
        id="entitlement-1",
        user_id="user-1",
        package_id="package-1",
        status="ACTIVE",
        term=EntitlementTerm.MONTH_1,
        granted_at=NOW - timedelta(days=20),
        expires_at=NOW + timedelta(days=10),
        version=1,
    )
    service = FormalEntitlementService(repository)

    renewed = await service.apply_operation(
        command(EntitlementOperation.RENEW, EntitlementTerm.MONTH_1),
        "admin-1",
        "renew-1",
        NOW,
    )
    assert renewed.expires_at == datetime(2026, 11, 8, 14, 0, tzinfo=UTC)

    renewed.expires_at = NOW - timedelta(seconds=1)
    regranted = await service.apply_operation(
        command(EntitlementOperation.RENEW, EntitlementTerm.MONTH_1),
        "admin-1",
        "renew-2",
        NOW,
    )
    assert regranted.expires_at == datetime(2026, 10, 28, 14, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_permanent_cannot_be_stacked() -> None:
    # 功能:验证永久权益不能叠加期限。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    repository = InMemoryFormalEntitlementRepository()
    repository.entitlements[("user-1", "package-1")] = FormalEntitlement(
        id="entitlement-1",
        user_id="user-1",
        package_id="package-1",
        status="ACTIVE",
        term=EntitlementTerm.PERMANENT,
        granted_at=NOW,
        expires_at=None,
        version=1,
    )
    service = FormalEntitlementService(repository)

    with pytest.raises(AppError) as exc:
        await service.apply_operation(
            command(EntitlementOperation.RENEW, EntitlementTerm.MONTH_1),
            "admin-1",
            "renew-permanent",
            NOW,
        )
    assert exc.value.code == "PERMANENT_ENTITLEMENT_CANNOT_EXTEND"


@pytest.mark.asyncio
async def test_pause_and_resume_do_not_extend_expiry() -> None:
    # 功能:验证暂停及恢复不会延长正式权益到期日。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    repository = InMemoryFormalEntitlementRepository()
    service = FormalEntitlementService(repository)
    granted = await service.apply_operation(
        command(EntitlementOperation.GRANT, EntitlementTerm.MONTH_3),
        "admin-1",
        "grant-1",
        NOW,
    )
    original_expiry = granted.expires_at

    paused = await service.apply_operation(
        command(EntitlementOperation.PAUSE),
        "admin-1",
        "pause-1",
        NOW + timedelta(days=10),
    )
    resumed = await service.apply_operation(
        command(EntitlementOperation.RESUME),
        "admin-1",
        "resume-1",
        NOW + timedelta(days=20),
    )

    assert paused.status == "PAUSED"
    assert resumed.status == "ACTIVE"
    assert resumed.expires_at == original_expiry


@pytest.mark.asyncio
async def test_preview_and_apply_use_identical_calculation() -> None:
    # 功能:验证预览及执行采用相同权益计算。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    repository = InMemoryFormalEntitlementRepository()
    service = FormalEntitlementService(repository)
    operation = command(EntitlementOperation.GRANT, EntitlementTerm.MONTH_6)

    preview = await service.preview_operation(operation, NOW)
    applied = await service.apply_operation(operation, "admin-1", "grant-previewed", NOW)

    assert preview.status == applied.status
    assert preview.term == applied.term
    assert preview.expires_at == applied.expires_at


@pytest.mark.asyncio
async def test_same_idempotency_key_replays_and_different_request_conflicts() -> None:
    # 功能:验证同一幂等键可重放且不同请求冲突。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    repository = InMemoryFormalEntitlementRepository()
    service = FormalEntitlementService(repository)
    grant = command(EntitlementOperation.GRANT, EntitlementTerm.MONTH_1)

    first = await service.apply_operation(grant, "admin-1", "same-key", NOW)
    replay = await service.apply_operation(grant, "admin-1", "same-key", NOW)
    assert first == replay
    assert replay.version == 1

    with pytest.raises(AppError) as exc:
        await service.apply_operation(
            command(EntitlementOperation.GRANT, EntitlementTerm.MONTH_2),
            "admin-1",
            "same-key",
            NOW,
        )
    assert exc.value.code == "IDEMPOTENCY_KEY_REUSED"
