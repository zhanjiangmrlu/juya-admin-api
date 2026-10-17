from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from juya_admin_api.modules.content.v15_authoring_router import create_authoring_router


@pytest.mark.asyncio
async def test_current_slice_state_read_requires_version_and_is_no_store():
    calls = []

    class Store:
        async def slice_state(self, revision, sentence, expected_version):
            calls.append((revision, sentence, expected_version))
            return {"status": "PREPARING", "resource_version": None}

    async def admin():
        return SimpleNamespace(admin_user_id=1)

    app = FastAPI()
    app.include_router(create_authoring_router(Store(), current_admin_write=admin))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        response = await c.get(
            "/api/v1/admin/content/revisions/r/sentences/s/slice-state",
            params={"expected_version": 3},
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["resource_version"] is None
        assert calls == [("r", "s", 3)]
        assert (
            await c.get("/api/v1/admin/content/revisions/r/sentences/s/slice-state")
        ).status_code == 422
