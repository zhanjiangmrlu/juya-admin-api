from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from juya_admin_api.modules.content.production_router import create_production_content_router


def test_ocr_suggestion_and_adoption_routes_are_retired() -> None:
    content = AsyncMock()
    media_admin = AsyncMock()

    async def admin():
        return Mock()

    app = FastAPI()
    app.include_router(
        create_production_content_router(
            Mock(),
            content,
            Mock(),
            media_admin,
            audit_service=Mock(),
            current_admin=admin,
            current_admin_write=admin,
        )
    )
    assert all("ocr" not in path.lower() for path in app.openapi()["paths"])
    with TestClient(app) as client:
        assert (
            client.get("/api/v1/admin/content/revisions/rev/ocr-suggestions/job").status_code == 404
        )
        assert (
            client.post("/api/v1/admin/content/revisions/rev/ocr-adoptions", json={}).status_code
            == 404
        )
    assert not content.mock_calls and not media_admin.mock_calls
