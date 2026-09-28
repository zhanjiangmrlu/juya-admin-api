import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.main import create_app
from juya_admin_api.shared.errors import AppError


def make_app() -> FastAPI:
    app = create_app(Settings(environment="test"))
    return app


@pytest.mark.asyncio
async def test_live_health_returns_request_id() -> None:
    app = make_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "juya-admin-api"}
    assert response.headers["X-Request-ID"]


@pytest.mark.asyncio
async def test_ready_health_uses_replaceable_probe() -> None:
    async def unavailable_database() -> dict[str, bool]:
        return {"mysql": False}

    app = create_app(Settings(environment="test"), readiness_probe=unavailable_database)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/ready", headers={"X-Request-ID": "ready-123"})

    assert response.status_code == 503
    assert response.json() == {
        "code": "SERVICE_NOT_READY",
        "message": "服务尚未就绪",
        "request_id": "ready-123",
        "details": {"checks": {"mysql": False}},
    }


@pytest.mark.asyncio
async def test_app_error_uses_safe_shape() -> None:
    app = make_app()

    @app.get("/boom")
    async def boom() -> None:
        raise AppError(
            code="STATE_CONFLICT",
            message="当前状态不允许此操作",
            status_code=409,
            details={"allowed_actions": ["resume"]},
        )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/boom", headers={"X-Request-ID": "request-123"})

    assert response.status_code == 409
    assert response.json() == {
        "code": "STATE_CONFLICT",
        "message": "当前状态不允许此操作",
        "request_id": "request-123",
        "details": {"allowed_actions": ["resume"]},
    }
    assert response.headers["X-Request-ID"] == "request-123"


@pytest.mark.asyncio
async def test_validation_error_does_not_expose_input() -> None:
    app = make_app()

    class Payload(BaseModel):
        count: int

    @app.post("/validated")
    async def validated(payload: Payload) -> Payload:
        return payload

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/validated",
            headers={"X-Request-ID": "request-456"},
            json={"count": "secret-value"},
        )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert body["message"] == "请求参数不正确"
    assert body["request_id"] == "request-456"
    assert body["details"]["errors"] == [
        {
            "type": "int_parsing",
            "loc": ["body", "count"],
            "msg": "Input should be a valid integer, unable to parse string as an integer",
        }
    ]
    assert "secret-value" not in response.text
