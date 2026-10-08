from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from juya_admin_api.modules.access_policy.router import create_internal_content_router


@pytest.mark.asyncio
async def test_internal_entitlements_preserves_activity_scene_membership() -> None:
    # 功能:验证内部权益投影保留活动与场景的归属关系。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    class Queries:
        async def entitlements(self, user_id: str, now: datetime) -> dict[str, object]:
            # 功能:返回预设权益投影,供内部接口契约检查。
            # 参数:
            #     self: 当前 Queries 测试替身实例,保存本用例的预设状态或调用记录。
            #     user_id: 目标用户标识;认证仓库中使用管理员数据库主键。
            #     now: 测试指定的当前时间,用于稳定计算期限、状态迁移和事件时间。
            # 返回:dict[str, object],由本用例预设的数据或所组装的测试资源构成。
            assert user_id == "user"
            assert now.tzinfo is not None
            return {
                "formal": [],
                "limited": [{"id": "gift", "scene_ids": ["scene-a"]}],
                "version": "v1",
                "server_now": now,
            }

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
            None, Queries(), current_service=principal, clock=lambda: datetime.now(UTC)
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post("/internal/v1/entitlements", json={"user_id": "user"})
    assert result.status_code == 200
    assert result.json()["limited"][0]["scene_ids"] == ["scene-a"]
