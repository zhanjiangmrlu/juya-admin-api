from datetime import UTC, datetime

import pytest

from juya_admin_api.integrations.miniapp_api.client import (
    ContactCorrection,
    ContactCorrectionPage,
    ContactProjection,
    ContactTimelineEvent,
    CorrectionDecision,
)
from juya_admin_api.modules.audit.service import AuditEvent, AuditService
from juya_admin_api.shared.errors import AppError

NOW = datetime(2026, 9, 29, 1, 0, tzinfo=UTC)


class RecordingAuditRepository:
    def __init__(self, *, fail: bool = False) -> None:
        # 功能:初始化 RecordingAuditRepository 测试替身的预设数据和调用记录。
        # 参数:
        #     self: 当前 RecordingAuditRepository 测试替身实例,保存本用例的预设状态或调用记录。
        #     fail: 是否让替身抛出错误,以检查失败分支。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.events: list[AuditEvent] = []
        self.fail = fail

    async def append(self, event: AuditEvent) -> None:
        # 功能:向测试仓库追加审计事件,供后续断言操作次数和内容。
        # 参数:
        #     self: 当前 RecordingAuditRepository 测试替身实例,保存本用例的预设状态或调用记录。
        #     event: 待记录的审计或业务事件。
        # 返回:无;仅 self.fail 为真时模拟审计存储失败。
        if self.fail:
            raise RuntimeError("audit unavailable")
        self.events.append(event)

    async def list_recent(self, limit: int) -> list[AuditEvent]:
        # 功能:从测试仓库返回最近的指定数量审计事件。
        # 参数:
        #     self: 当前 RecordingAuditRepository 测试替身实例,保存本用例的预设状态或调用记录。
        #     limit: 最多返回的记录数,用于最近审计或任务批量处理。
        # 返回:list[AuditEvent],由本用例预设的数据或所组装的测试资源构成。
        return self.events[-limit:]


class FakeMiniappClient:
    def __init__(self) -> None:
        # 功能:初始化 FakeMiniappClient 测试替身的预设数据和调用记录。
        # 参数:
        #     self: 当前 FakeMiniappClient 测试替身实例,保存本用例的预设状态或调用记录。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.fail_update = False
        self.calls: list[tuple[object, ...]] = []
        self.correction = ContactCorrection(
            id="correction-1",
            user_id="user-1",
            juya_number="JY000000000001",
            nickname="学习者",
            wechat_id="wx-private",
            reason="微信号需要更正",
            status="PENDING",
            created_at=NOW,
            processed_at=None,
            timeline=(
                ContactTimelineEvent(
                    "PENDING",
                    "USER",
                    "user-1",
                    "CONTACT_CORRECTION_CREATED",
                    NOW,
                ),
            ),
        )

    async def list_contact_corrections(
        self, status: str | None, page: int, page_size: int, admin_id: str
    ) -> ContactCorrectionPage:
        # 功能:返回联系方式修正申请列表的预设分页结果。
        # 参数:
        #     self: 当前 FakeMiniappClient 测试替身实例,保存本用例的预设状态或调用记录。
        #     status: 本测试预设的业务状态或返回状态,用于检查状态约束。
        #     page: 分页页码,从第一页开始。
        #     page_size: 每页最多返回的记录数。
        #     admin_id: 执行操作的管理员标识,供权限上下文及审计归属检查。
        # 返回:ContactCorrectionPage,由本用例预设的数据或所组装的测试资源构成。
        self.calls.append(("list", status, page, page_size, admin_id))
        return ContactCorrectionPage((self.correction,), 1, page, page_size)

    async def get_contact_correction(self, correction_id: str, admin_id: str) -> ContactCorrection:
        # 功能:返回指定修正申请的预设详情。
        # 参数:
        #     self: 当前 FakeMiniappClient 测试替身实例,保存本用例的预设状态或调用记录。
        #     correction_id: 待查询或处理的联系方式修正申请标识。
        #     admin_id: 执行操作的管理员标识,供权限上下文及审计归属检查。
        # 返回:ContactCorrection,由本用例预设的数据或所组装的测试资源构成。
        self.calls.append(("detail", correction_id, admin_id))
        return self.correction

    async def update_contact_status(
        self, user_id: str, status: str, admin_id: str
    ) -> ContactProjection:
        # 功能:模拟更新联系方式状态并记录调用参数。
        # 参数:
        #     self: 当前 FakeMiniappClient 测试替身实例,保存本用例的预设状态或调用记录。
        #     user_id: 目标用户标识;认证仓库中使用管理员数据库主键。
        #     status: 本测试预设的业务状态或返回状态,用于检查状态约束。
        #     admin_id: 执行操作的管理员标识,供权限上下文及审计归属检查。
        # 返回:ContactProjection,由本用例预设的数据或所组装的测试资源构成。
        self.calls.append(("status", user_id, status, admin_id))
        if self.fail_update:
            raise AppError("MINIAPP_API_UNAVAILABLE", "unavailable", 503)
        return ContactProjection(user_id, "wx-private", status, False, None, None, NOW)

    async def verify_contact_change(self, user_id: str, admin_id: str) -> ContactProjection:
        # 功能:模拟核实联系方式修改并返回预设投影。
        # 参数:
        #     self: 当前 FakeMiniappClient 测试替身实例,保存本用例的预设状态或调用记录。
        #     user_id: 目标用户标识;认证仓库中使用管理员数据库主键。
        #     admin_id: 执行操作的管理员标识,供权限上下文及审计归属检查。
        # 返回:ContactProjection,由本用例预设的数据或所组装的测试资源构成。
        self.calls.append(("verify", user_id, admin_id))
        return ContactProjection(user_id, "wx-private", "PENDING", False, NOW, admin_id, NOW)

    async def decide_contact_correction(
        self,
        correction_id: str,
        decision: str,
        admin_id: str,
        idempotency_key: str,
    ) -> CorrectionDecision:
        # 功能:模拟审批联系方式修正申请并返回审批结果。
        # 参数:
        #     self: 当前 FakeMiniappClient 测试替身实例,保存本用例的预设状态或调用记录。
        #     correction_id: 待查询或处理的联系方式修正申请标识。
        #     decision: 联系方式修正审批结论,例如批准或拒绝。
        #     admin_id: 执行操作的管理员标识,供权限上下文及审计归属检查。
        #     idempotency_key: 请求幂等键,用于匹配原请求并避免重复业务写入。
        # 返回:CorrectionDecision,由本用例预设的数据或所组装的测试资源构成。
        self.calls.append(("decision", correction_id, decision, admin_id, idempotency_key))
        return CorrectionDecision(correction_id, decision, NOW)


@pytest.mark.asyncio
async def test_successful_contact_actions_write_only_redacted_audits() -> None:
    # 功能:验证成功联系方式操作只写脱敏审计。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    from juya_admin_api.modules.contacts.service import ContactAdminService

    audit_repository = RecordingAuditRepository()
    client = FakeMiniappClient()
    service = ContactAdminService(client, AuditService(audit_repository))

    await service.list_corrections("PENDING", 1, 20, "admin-1", "request-list", NOW)
    await service.get_correction("correction-1", "admin-1", "request-detail", NOW)
    await service.update_status("user-1", "CONTACTED", "admin-1", "request-status", NOW)
    await service.verify_change("user-1", "admin-1", "request-verify", NOW)
    await service.decide_correction(
        "correction-1",
        "APPROVED",
        "admin-1",
        "idem-1",
        "request-decision",
        NOW,
    )
    await service.audit_copy("user-1", "admin-1", "request-copy", NOW)

    assert [event.action for event in audit_repository.events] == [
        "contact.view",
        "contact.view",
        "contact.status.update",
        "contact.change.verify",
        "contact.correction.approve",
        "contact.copy",
    ]
    serialized = repr(audit_repository.events)
    assert "wx-private" not in serialized
    assert "微信号需要更正" not in serialized
    assert audit_repository.events[2].after_summary == {"status": "CONTACTED"}


@pytest.mark.asyncio
async def test_upstream_failure_does_not_write_success_audit() -> None:
    # 功能:验证上游失败不会写成功审计。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    from juya_admin_api.modules.contacts.service import ContactAdminService

    audit_repository = RecordingAuditRepository()
    client = FakeMiniappClient()
    client.fail_update = True
    service = ContactAdminService(client, AuditService(audit_repository))

    with pytest.raises(AppError):
        await service.update_status("user-1", "CONTACTED", "admin-1", "request-status", NOW)

    assert audit_repository.events == []


@pytest.mark.asyncio
async def test_copy_is_not_reported_successful_when_audit_storage_fails() -> None:
    # 功能:验证审计存储失败时复制操作不报告成功。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    from juya_admin_api.modules.contacts.service import ContactAdminService

    service = ContactAdminService(
        FakeMiniappClient(),
        AuditService(RecordingAuditRepository(fail=True)),
    )

    with pytest.raises(RuntimeError, match="audit unavailable"):
        await service.audit_copy("user-1", "admin-1", "request-copy", NOW)
