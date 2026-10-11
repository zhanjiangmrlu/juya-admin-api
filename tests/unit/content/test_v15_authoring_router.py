from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from juya_admin_api.modules.content.v15_authoring_router import create_authoring_router


@pytest.mark.asyncio
async def test_slice_preview_admin_read_dependency_and_no_store():
    async def admin():
        return SimpleNamespace(admin_user_id=1)

    store = SimpleNamespace(
        slice_preview=AsyncMock(
            return_value={
                "url": "https://private.invalid/signed",
                "expires_in": 60,
                "status": "READY",
            }
        )
    )
    app = FastAPI()
    app.include_router(
        create_authoring_router(store, current_admin_write=admin, current_admin=admin)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/v1/admin/content/revisions/r/sentences/s/slice-preview",
            params={"dependency_hash": "a" * 64},
        )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["expires_in"] == 60
    assert response.json()["status"] == "READY"
    store.slice_preview.assert_awaited_once_with("r", "s", "a" * 64)


@pytest.mark.asyncio
async def test_slice_preview_rejects_nonadmin_before_resource_lookup():
    async def denied():
        raise HTTPException(status_code=403)

    store = SimpleNamespace(slice_preview=AsyncMock())
    app = FastAPI()
    app.include_router(
        create_authoring_router(store, current_admin_write=denied, current_admin=denied)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/v1/admin/content/revisions/r/sentences/s/slice-preview",
            params={"dependency_hash": "a" * 64},
        )
    assert response.status_code == 403
    store.slice_preview.assert_not_awaited()
