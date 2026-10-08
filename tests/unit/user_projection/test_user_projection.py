from datetime import UTC, datetime

import pytest

from juya_admin_api.integrations.miniapp_api.client import (
    ContactProjection,
    ContactProjectionResult,
    LearningOverview,
)
from juya_admin_api.modules.audit.service import AuditEvent, AuditService
from juya_admin_api.modules.user_projection.service import (
    InMemoryUserProjectionRepository,
    UserProjection,
    UserProjectionService,
)
from juya_admin_api.shared.errors import AppError

NOW = datetime(2026, 9, 29, 3, 0, tzinfo=UTC)


class RecordingAuditRepository:
    def __init__(self) -> None:
        # 功能:初始化 RecordingAuditRepository 测试替身的预设数据和调用记录。
        # 参数:
        #     self: 当前 RecordingAuditRepository 测试替身实例,保存本用例的预设状态或调用记录。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.events: list[AuditEvent] = []

    async def append(self, event: AuditEvent) -> None:
        # 功能:向测试仓库追加审计事件,供后续断言操作次数和内容。
        # 参数:
        #     self: 当前 RecordingAuditRepository 测试替身实例,保存本用例的预设状态或调用记录。
        #     event: 待记录的审计或业务事件。
        # 返回:无;完成模拟状态更新、调用记录或检查。
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
        self.contact_degraded = False
        self.learning_unavailable = False
        self.calls: list[tuple[object, ...]] = []
        self.contacts = (
            ContactProjection("user-1", "wx-private", "CONTACTED", False, NOW, "admin-0", NOW),
            ContactProjection("user-2", None, "PENDING", True, None, None, NOW),
        )

    async def search_user_ids_by_wechat(
        self, wechat_id: str, admin_id: str = "system"
    ) -> tuple[str, ...]:
        # 功能:按微信号返回预设匹配用户标识并记录管理员上下文。
        # 参数:
        #     self: 当前 FakeMiniappClient 测试替身实例,保存本用例的预设状态或调用记录。
        #     wechat_id: 被搜索的微信号,模拟上游联系方式查询。
        #     admin_id: 执行操作的管理员标识,供权限上下文及审计归属检查。
        # 返回:tuple[str, ...],由本用例预设的数据或所组装的测试资源构成。
        self.calls.append(("wechat", wechat_id, admin_id))
        return ("user-1",)

    async def get_contact_projections(
        self, user_ids: tuple[str, ...], admin_id: str = "system"
    ) -> ContactProjectionResult:
        # 功能:按用户标识批量返回预设联系方式投影。
        # 参数:
        #     self: 当前 FakeMiniappClient 测试替身实例,保存本用例的预设状态或调用记录。
        #     user_ids: 需要批量获取联系方式投影的用户标识集合。
        #     admin_id: 执行操作的管理员标识,供权限上下文及审计归属检查。
        # 返回:ContactProjectionResult,由本用例预设的数据或所组装的测试资源构成。
        self.calls.append(("contacts", user_ids, admin_id))
        if self.contact_degraded:
            return ContactProjectionResult((), True)
        allowed = frozenset(user_ids)
        return ContactProjectionResult(
            tuple(item for item in self.contacts if item.user_id in allowed), False
        )

    async def get_learning_overview(self, user_id: str, admin_id: str) -> LearningOverview:
        # 功能:返回测试用户的预设学习汇总信息。
        # 参数:
        #     self: 当前 FakeMiniappClient 测试替身实例,保存本用例的预设状态或调用记录。
        #     user_id: 目标用户标识;认证仓库中使用管理员数据库主键。
        #     admin_id: 执行操作的管理员标识,供权限上下文及审计归属检查。
        # 返回:LearningOverview,由本用例预设的数据或所组装的测试资源构成。
        self.calls.append(("learning", user_id, admin_id))
        if self.learning_unavailable:
            raise AppError("MINIAPP_API_UNAVAILABLE", "unavailable", 503)
        return LearningOverview(7, 12, 3)


def _service() -> tuple[
    UserProjectionService,
    FakeMiniappClient,
    RecordingAuditRepository,
]:
    # 功能:组装用户投影服务、上游替身及可记录审计仓库。
    # 参数:无。
    # 返回:tuple[UserProjectionService, FakeMiniappClient, RecordingAuditRepository],由本用例预
    #       设的数据或所组装的测试资源构成。
    repository = InMemoryUserProjectionRepository()
    repository.users = {
        "user-1": UserProjection("user-1", "ACTIVE", NOW, 1, 2, 0, contact_status="CONTACTED"),
        "user-2": UserProjection("user-2", "ACTIVE", NOW, 0, 1, 1, contact_status="PENDING"),
    }
    client = FakeMiniappClient()
    audits = RecordingAuditRepository()
    return UserProjectionService(repository, client, AuditService(audits)), client, audits


@pytest.mark.asyncio
async def test_search_batches_contacts_filters_server_side_and_audits_sensitive_rows() -> None:
    # 功能:验证搜索批量获取联系方式、服务端筛选并审计敏感行。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    service, client, audits = _service()

    result = await service.search(
        contact_status="CONTACTED",
        admin_id="admin-1",
        request_id="request-list",
        occurred_at=NOW,
    )

    assert [item.projection.user_id for item in result] == ["user-1"]
    assert result[0].contact == client.contacts[0]
    assert client.calls == [("contacts", ("user-1",), "admin-1")]
    assert [event.action for event in audits.events] == ["contact.view.list"]
    assert audits.events[0].after_summary == {
        "hit_count": 1,
        "user_ids": ["user-1"],
    }
    assert "wx-private" not in repr(audits.events)


@pytest.mark.asyncio
async def test_search_by_wechat_uses_admin_call_and_keeps_local_order() -> None:
    # 功能:验证微信号搜索使用管理员调用且保留本地顺序。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    service, client, _audits = _service()

    result = await service.search(
        wechat_id="wx-private",
        admin_id="admin-1",
        request_id="request-search",
        occurred_at=NOW,
    )

    assert [item.projection.user_id for item in result] == ["user-1"]
    assert client.calls[:2] == [
        ("wechat", "wx-private", "admin-1"),
        ("contacts", ("user-1",), "admin-1"),
    ]


@pytest.mark.asyncio
async def test_unfiltered_search_degrades_without_fabricating_contacts() -> None:
    # 功能:验证未筛选搜索降级时不伪造联系方式。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    service, client, audits = _service()
    client.contact_degraded = True

    result = await service.search(
        admin_id="admin-1",
        request_id="request-list",
        occurred_at=NOW,
    )

    assert len(result) == 2
    assert all(item.contact is None and item.contact_degraded for item in result)
    assert audits.events == []

    with pytest.raises(AppError, match="联系资料") as captured:
        await service.search(
            contact_status="CONTACTED",
            admin_id="admin-1",
            request_id="request-filter",
            occurred_at=NOW,
        )
    assert captured.value.code == "MINIAPP_API_UNAVAILABLE"


@pytest.mark.asyncio
async def test_detail_aggregates_contact_and_learning_and_audits_after_success() -> None:
    # 功能:验证详情聚合联系方式和学习信息且成功后写审计。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    service, client, audits = _service()

    detail = await service.detail(
        "user-1",
        admin_id="admin-1",
        request_id="request-detail",
        occurred_at=NOW,
    )

    assert detail.contact == client.contacts[0]
    assert detail.contact_degraded is False
    assert detail.learning_overview == LearningOverview(7, 12, 3)
    assert detail.learning_degraded is False
    assert [event.action for event in audits.events] == ["contact.view.detail"]
    assert audits.events[0].after_summary == {"hit_count": 1, "user_id": "user-1"}
    assert "wx-private" not in repr(audits.events)


@pytest.mark.asyncio
async def test_detail_learning_unavailable_is_explicitly_degraded() -> None:
    # 功能:验证学习详情不可用时明确报告降级。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    service, client, _audits = _service()
    client.learning_unavailable = True

    detail = await service.detail(
        "user-1",
        admin_id="admin-1",
        request_id="request-detail",
        occurred_at=NOW,
    )

    assert detail.learning_overview is None
    assert detail.learning_degraded is True
