import json
from datetime import UTC, datetime
from typing import Any

import pytest

from juya_admin_api.modules.campaigns.repository import SQLAlchemyCampaignRepository


@pytest.mark.asyncio
async def test_campaign_audit_summary_omits_names_and_unbounded_scene_ids() -> None:
    # 功能:验证活动审计摘要忽略姓名及不受限的场景标识列表。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    class Session:
        def __init__(self) -> None:
            # 功能:初始化 Session 测试替身的预设数据和调用记录。
            # 参数:
            #     self: 当前 Session 测试替身实例,保存本用例的预设状态或调用记录。
            # 返回:无;完成模拟状态更新、调用记录或检查。
            self.params: dict[str, Any] = {}

        async def execute(self, _statement: object, params: dict[str, Any]) -> None:
            # 功能:模拟 SQL 执行并返回预设结果或更新内存配置。
            # 参数:
            #     self: 当前 Session 测试替身实例,保存本用例的预设状态或调用记录。
            #     _statement: 即将执行的 SQL 语句,用于选择故障注入位置。 当前替身保留该形参以兼
            #       容调用接口。
            #     params: 调用方传给被替换仓库方法的关键字参数映射。
            # 返回:无;完成模拟状态更新、调用记录或检查。
            self.params = params

    session = Session()
    repository = SQLAlchemyCampaignRepository(None)  # type: ignore[arg-type]
    after = {
        "id": "campaign-1",
        "name": "private campaign name",
        "status": "OPEN",
        "version": 2,
        "current_version": {
            "id": "version-1",
            "capacity": 10,
            "granted_user_count": 1,
            "scene_ids": ["scene-" + str(i) for i in range(1000)],
        },
    }
    await repository._audit(
        session, 1, "OPEN", None, after, "admin-1", datetime(2026, 9, 29, tzinfo=UTC)
    )  # type: ignore[arg-type]
    summary = json.loads(session.params["after"])
    assert summary == {
        "id": "campaign-1",
        "status": "OPEN",
        "version": 2,
        "current_version_id": "version-1",
        "capacity": 10,
        "granted_user_count": 1,
    }
