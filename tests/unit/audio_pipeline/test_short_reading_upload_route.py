import httpx
import pytest
from fastapi import FastAPI

from juya_admin_api.modules.short_reading.upload_targets import create_short_reading_upload_router


@pytest.mark.asyncio
async def test_upload_target_requires_idempotency_and_cas_versions():
    async def admin():
        return None

    app = FastAPI()
    app.include_router(create_short_reading_upload_router(None, None, current_admin_write=admin))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        response = await c.post(
            "/api/v1/admin/content/short-readings/c/revisions/r/audio-upload-target",
            json={"expected_version": 1, "expected_edit_version": 1},
        )
        assert response.status_code == 422
        assert (
            await c.post(
                "/api/v1/admin/content/short-readings/c/revisions/r/audio-upload-target",
                headers={"Idempotency-Key": "test"},
                json={"expected_version": 1},
            )
        ).status_code == 422
