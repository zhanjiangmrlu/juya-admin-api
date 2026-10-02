from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from juya_admin_api.modules.feedback.repository import InMemoryFeedbackRepository
from juya_admin_api.modules.feedback.router import create_internal_feedback_router
from juya_admin_api.modules.feedback.service import FeedbackService


@pytest.mark.asyncio
async def test_user_feedback_query_and_detail_keep_public_history_only() -> None:
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
        return object()

    app = FastAPI()
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
