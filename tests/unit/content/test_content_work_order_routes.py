import importlib.util
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from juya_admin_api.shared.errors import AppError, install_error_handlers


def test_work_order_routes_are_typed_and_writes_require_write_permission():
    assert importlib.util.find_spec("juya_admin_api.modules.content_production.router"), (
        "typed work-order router missing"
    )
    from juya_admin_api.modules.content_production.router import create_work_order_router

    async def read():
        return SimpleNamespace(admin_user_id="reader")

    async def write():
        raise AppError("ADMIN_WRITE_FORBIDDEN", "只读管理员", 403)

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(
        create_work_order_router(SimpleNamespace(), current_admin=read, current_admin_write=write)
    )
    paths = app.openapi()["paths"]
    base = "/api/v1/admin/content-production/work-orders"
    assert base in paths and base + "/{work_order_id}/items/{item_id}" in paths
    assert base + "/{work_order_id}/commands/{command_id}/resume" in paths
    for action in ("candidates", "candidate-decisions", "entry-confirmations", "usage"):
        assert base + "/{work_order_id}/" + action in paths
    for path, operations in paths.items():
        for method, operation in operations.items():
            if method not in {"get", "post", "put"} or path.endswith("/original-file"):
                continue
            responses = operation["responses"]
            response = responses.get("200") or responses.get("202")
            assert "$ref" in response["content"]["application/json"]["schema"], path
    with TestClient(app) as client:
        response = client.post(
            base + "/from-batch", json={"batch_id": "one"}, headers={"Idempotency-Key": "x"}
        )
        assert response.status_code == 403
        response = client.post(
            base + "/one/sampling", json={"expected_version": 1}, headers={"Idempotency-Key": "x"}
        )
        assert response.status_code == 403
    current = app.openapi()["components"]["schemas"]["WorkItemDetail"]["properties"]["current"]
    assert current["anyOf"][0]["discriminator"]["propertyName"] == "template"
