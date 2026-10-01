from datetime import UTC, datetime, timedelta

import pytest

from juya_admin_api.modules.feedback.repository import InMemoryFeedbackRepository
from juya_admin_api.modules.feedback.service import FeedbackService


@pytest.mark.asyncio
async def test_overdue_feedback_precedes_recent_urgent_rows_before_pagination() -> None:
    now = datetime(2026, 10, 1, tzinfo=UTC)
    repo = InMemoryFeedbackRepository()
    service = FeedbackService(repo)
    overdue = await service.create("u", "CONTENT", "old", {}, [], "old", now - timedelta(days=3))
    for index in range(21):
        await service.create(
            "u", "CONTENT", "recent", {}, [], f"new-{index}", now - timedelta(hours=40)
        )
    page = await service.list_admin({"sla": "URGENT"}, 1, 20, now)
    assert page.total == 22
    assert page.items[0].id == overdue.id
