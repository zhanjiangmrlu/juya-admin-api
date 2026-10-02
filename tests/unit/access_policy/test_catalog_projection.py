from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from juya_admin_api.modules.access_policy.domain import AccessDecision, AccessLevel
from juya_admin_api.modules.access_policy.router import create_internal_content_router


@pytest.mark.asyncio
async def test_catalog_has_display_fields_and_only_visible_authorized_summaries() -> None:
    class Queries:
        async def learning_catalog(self, _: str) -> list[dict[str, object]]:
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
            return AccessDecision(AccessLevel(scene_id.upper()), (), None)

    async def principal() -> object:
        return object()

    app = FastAPI()
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
