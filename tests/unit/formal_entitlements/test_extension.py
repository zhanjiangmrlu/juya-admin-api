"""Formal extension preserves existing state and requires an auditable reason."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from juya_admin_api.modules.formal_entitlements.domain import (
    EntitlementOperation,
    EntitlementTerm,
    FormalEntitlement,
    FormalEntitlementCommand,
    calculate_operation,
)
from juya_admin_api.modules.formal_entitlements.repository import (
    InMemoryFormalEntitlementRepository,
)
from juya_admin_api.modules.formal_entitlements.service import FormalEntitlementService
from juya_admin_api.shared.errors import AppError

NOW = datetime(2026, 1, 10, 5, 6, tzinfo=UTC)
EXPIRES = datetime(2026, 1, 31, 5, 6, tzinfo=UTC)


def current(status: str = "ACTIVE") -> FormalEntitlement:
    # status 指定已有正式权益状态,其他固定事实用于检查自然月与状态保留
    return FormalEntitlement(
        "existing",
        "user",
        "package",
        status,
        EntitlementTerm.MONTH_1,
        datetime(2025, 12, 31, 5, 6, tzinfo=UTC),
        EXPIRES,
        7,
    )


def extension(
    reason: str | None = "服务补偿", term: EntitlementTerm | None = EntitlementTerm.MONTH_1
):
    # reason 是延期审计原因,term 是有限自然月延期档位
    return FormalEntitlementCommand(
        "user", "package", EntitlementOperation("EXTEND"), term, reason, expected_version=7
    )


@pytest.mark.parametrize("status", ["ACTIVE", "PAUSED"])
def test_extension_preserves_access_status_and_grant_time_at_month_end(status):
    # status 覆盖正常和暂停权益,延期不能恢复已暂停内容权限
    before = current(status)
    after = calculate_operation(before, extension(), NOW)
    assert after.expires_at == datetime(2026, 2, 28, 5, 6, tzinfo=UTC)
    assert after.status == status and after.granted_at == before.granted_at
    assert after.id == "existing" and after.version == 8


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_extension_requires_reason(reason):
    # reason 覆盖缺失与空白延期原因,必须拒绝无依据的运营补偿
    with pytest.raises(AppError) as caught:
        calculate_operation(current(), extension(reason), NOW)
    assert caught.value.code == "ENTITLEMENT_REASON_REQUIRED"


@pytest.mark.parametrize(
    "before",
    [
        None,
        current("REVOKED"),
        current("EXPIRED"),
        replace(current(), expires_at=NOW),
        replace(current(), term=EntitlementTerm.PERMANENT, expires_at=None),
    ],
)
def test_extension_cannot_create_reopen_or_extend_permanent_entitlement(before):
    # before 覆盖不存在、已撤销、已到期和永久权益,延期不应隐式新开通
    with pytest.raises(AppError):
        calculate_operation(before, extension(), NOW)


@pytest.mark.parametrize("term", [None, EntitlementTerm.PERMANENT])
def test_extension_requires_finite_month_term(term):
    # term 覆盖缺失及永久档位,延期只接受明确的有限自然月
    with pytest.raises(AppError):
        calculate_operation(current(), extension(term=term), NOW)


@pytest.mark.asyncio
async def test_extension_preview_and_idempotent_execution_share_audit_action():
    # 延期预览与执行使用真实服务和内存仓库,重试只能增加一次到期时间
    repository = InMemoryFormalEntitlementRepository()
    repository.entitlements[("user", "package")] = current("PAUSED")
    service = FormalEntitlementService(repository)
    command = extension()
    preview = await service.preview_details(command, NOW)
    assert preview["actual_operation"] == "EXTEND"
    assert preview["before"]["expires_at"] == EXPIRES
    first = await service.apply_operation(command, "admin", "extension-key", NOW)
    repeated = await service.apply_operation(command, "admin", "extension-key", NOW)
    assert first == repeated and first.version == 8
    assert first.status == "PAUSED" and first.expires_at == preview["after"]["expires_at"]
