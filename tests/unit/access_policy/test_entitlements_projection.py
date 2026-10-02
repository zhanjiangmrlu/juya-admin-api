from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from juya_admin_api.modules.access_policy.router import create_internal_content_router


@pytest.mark.asyncio
async def test_internal_entitlements_preserves_activity_scene_membership() -> None:
    class Queries:
        async def entitlements(self, user_id: str, now: datetime) -> dict[str, object]:
            assert user_id == "user"
            assert now.tzinfo is not None
            return {
                "formal": [],
                "limited": [{"id": "gift", "scene_ids": ["scene-a"]}],
                "version": "v1",
                "server_now": now,
            }

    async def principal() -> object:
        return object()

    app = FastAPI()
    app.include_router(
        create_internal_content_router(
            None, Queries(), current_service=principal, clock=lambda: datetime.now(UTC)
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post("/internal/v1/entitlements", json={"user_id": "user"})
    assert result.status_code == 200
    assert result.json()["limited"][0]["scene_ids"] == ["scene-a"]
