from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from juya_admin_api.modules.access_policy.domain import AccessDecision, AccessLevel
from juya_admin_api.modules.access_policy.router import create_internal_content_router


@pytest.mark.asyncio
async def test_catalog_has_display_fields_and_only_visible_authorized_summaries() -> None:
    # 功能:验证目录包含展示字段且仅返回可见的已授权摘要。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    class Queries:
        async def learning_catalog(self, _: str) -> list[dict[str, object]]:
            # 功能:返回目录投影预设数据,供字段与可见性检查。
            # 参数:
            #     self: 当前 Queries 测试替身实例,保存本用例的预设状态或调用记录。
            #     _: 调用协议要求保留但本测试不读取的位置参数。
            # 返回:list[dict[str, object]],由本用例预设的数据或所组装的测试资源构成。
            return [
                {
                    "public_id": key,
                    "title": "咖啡店",
                    "title_en": "Coffee",
                    "series": "日常英语",
                    "trial_sentence": "Could I get a latte, please?",
                }
                for key in ("open", "preview", "hidden")
            ]

    class Policy:
        async def authorize(self, _: str, scene_id: str, now: datetime) -> AccessDecision:
            # 功能:返回预设访问结论,供路由权限映射检查。
            # 参数:
            #     self: 当前 Policy 测试替身实例,保存本用例的预设状态或调用记录。
            #     _: 调用协议要求保留但本测试不读取的位置参数。
            #     scene_id: 目标学习场景标识。
            #     now: 测试指定的当前时间,用于稳定计算期限、状态迁移和事件时间。
            # 返回:AccessDecision,由本用例预设的数据或所组装的测试资源构成。
            return AccessDecision(AccessLevel(scene_id.upper()), (), None)

    async def principal() -> object:
        # 功能:提供测试所需的已认证主体占位对象。
        # 参数:无。
        # 返回:object,由本用例预设的数据或所组装的测试资源构成。
        return object()

    app = FastAPI()
    # 匿名函数: 注入固定测试时间或 UTC 当前时间, 控制接口和签名的时间源。
    # 参数: 无。
    # 返回: 对应测试时间或 UTC 当前时间。
    app.include_router(
        create_internal_content_router(
            Policy(), Queries(), current_service=principal, clock=lambda: datetime.now(UTC)
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post("/internal/v1/learning/catalog", json={"user_id": "user"})
    items = result.json()["items"]
    assert [item["scene_id"] for item in items] == ["open", "preview"]
    assert items[0]["title"] == "Coffee"
    assert items[0]["chinese_title"] == "咖啡店"
    assert items[0]["access"] == "OPEN"
    assert "content" not in items[1]
    assert items[0]["trial_sentence"] == "Could I get a latte, please?"
    assert "trial_sentence" not in items[1]
