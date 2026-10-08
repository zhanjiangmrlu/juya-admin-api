from datetime import UTC, datetime, timedelta

import pytest

from juya_admin_api.modules.feedback.repository import InMemoryFeedbackRepository
from juya_admin_api.modules.feedback.service import FeedbackService
from juya_admin_api.modules.system_config.service import (
    InMemorySystemConfigRepository,
    SystemConfigService,
)
from juya_admin_api.shared.errors import AppError


@pytest.mark.asyncio
async def test_feedback_reads_current_config_at_create_and_supply() -> None:
    # 功能:验证反馈创建及补充使用当前配置。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    config = SystemConfigService(
        InMemorySystemConfigRepository({"feedback_sla_hours": ({"value": 12}, 1)})
    )
    service = FeedbackService(
        InMemoryFeedbackRepository(), sla_hours_provider=config.feedback_sla_hours
    )
    now = datetime(2026, 10, 1, tzinfo=UTC)
    ticket = await service.create(
        "user", "CONTENT", "字幕问题", {"page": "scene", "scene_id": "scene-1"}, [], "create", now
    )
    assert ticket.deadline_at == now + timedelta(hours=12)
    await service.start_processing(ticket.id, "admin", "start", now)
    await service.request_supplement(ticket.id, "步骤", "admin", "more", now)
    await config.update("feedback_sla_hours", {"value": 36}, 1, "admin")
    supplied = await service.supply(ticket.id, "步骤补充", "user", "supply", now)
    assert supplied.deadline_at == now + timedelta(hours=36)
    page = await service.list_admin({}, 1, 20, now)
    assert page.items[0].source == {"page": "scene", "scene_id": "scene-1"}
    assert page.items[0].screenshot_status == "NONE"
    assert page.items[0].supplied_at == now


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,value",
    [
        ("feedback_sla_hours", 0),
        ("feedback_sla_hours", True),
        ("entitlement_expiry_warning_days", -1),
    ],
)
async def test_invalid_operations_config_is_rejected(key: str, value: object) -> None:
    # 功能:验证无效运营配置被拒绝。
    # 参数:
    #     key: 待更新运营配置的名称,用于选择对应校验规则。
    #     value: 本测试待验证的配置值或领域输入。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    config = SystemConfigService(InMemorySystemConfigRepository({key: ({"value": 7}, 1)}))
    with pytest.raises(AppError, match="配置"):
        await config.update(key, {"value": value}, 1, "admin")
