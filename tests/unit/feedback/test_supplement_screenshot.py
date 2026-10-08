from datetime import UTC, datetime

import pytest

from juya_admin_api.modules.feedback.repository import InMemoryFeedbackRepository
from juya_admin_api.modules.feedback.service import FeedbackService
from juya_admin_api.shared.errors import AppError


@pytest.mark.asyncio
async def test_supplement_adds_one_owned_image_and_replays_without_duplicate() -> None:
    # 功能:验证补充资料只新增一张自有图片且重放不重复。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    repo = InMemoryFeedbackRepository()
    service = FeedbackService(repo)
    now = datetime.now(UTC)
    ticket = await service.create("user", "CONTENT", "problem", {}, [], "create", now)
    await service.start_processing(ticket.id, "admin", "start", now)
    await service.request_supplement(ticket.id, "more", "admin", "request", now)
    await service.supply(
        ticket.id, "details", "user", "supply", now, screenshots=["feedback/user/image.png"]
    )
    await service.supply(
        ticket.id, "details", "user", "supply", now, screenshots=["feedback/user/image.png"]
    )
    assert repo.screenshots[ticket.id].object_key == "feedback/user/image.png"
    await service.start_processing(ticket.id, "admin", "start2", now)
    await service.request_supplement(ticket.id, "more", "admin", "request2", now)
    with pytest.raises(AppError) as error:
        await service.supply(
            ticket.id, "details", "user", "supply2", now, screenshots=["feedback/user/other.png"]
        )
    assert error.value.code == "FEEDBACK_SCREENSHOT_LIMIT"
    assert (await service.get(ticket.id)).status == "NEED_MORE"


@pytest.mark.asyncio
async def test_supplement_rejects_other_users_image() -> None:
    # 功能:验证补充资料拒绝其他用户的图片。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    service = FeedbackService(InMemoryFeedbackRepository())
    with pytest.raises(AppError) as error:
        await service.supply(
            "ticket",
            "details",
            "user",
            "supply",
            datetime.now(UTC),
            screenshots=["feedback/other/image.png"],
        )
    assert error.value.code == "FEEDBACK_SCREENSHOT_INVALID"
