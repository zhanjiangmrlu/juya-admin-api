from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from juya_admin_api.modules.feedback.repository import InMemoryFeedbackRepository
from juya_admin_api.modules.feedback.router import create_internal_feedback_router
from juya_admin_api.modules.feedback.service import FeedbackService


@pytest.mark.asyncio
async def test_user_feedback_query_and_detail_keep_public_history_only() -> None:
    # 功能:验证用户反馈查询及详情仅包含公开历史。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    now = datetime.now(UTC)
    service = FeedbackService(InMemoryFeedbackRepository())
    ticket = await service.create("user", "CONTENT", "problem", {}, [], "create", now)
    await service.create("other", "CONTENT", "private", {}, [], "create", now)
    await service.start_processing(ticket.id, "admin", "start", now)
    await service.request_supplement(ticket.id, "steps?", "admin", "request", now)
    await service.supply(
        ticket.id, "steps", "user", "supply", now, screenshots=["feedback/user/image.png"]
    )
    await service.add_internal_note(ticket.id, "admin", "internal-private-note", "note", now)

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
        create_internal_feedback_router(service, current_service=principal, clock=lambda: now)
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        query = await client.post("/internal/v1/feedback/query", json={"user_id": "user"})
        detail = await client.get(f"/internal/v1/feedback/{ticket.id}")
    assert query.status_code == 200
    assert [item["id"] for item in query.json()["items"]] == [ticket.id]
    assert detail.json()["supplements"][0]["text"] == "steps"
    assert detail.json()["screenshots"] == ["feedback/user/image.png"]
    assert "internal-private-note" not in detail.text
