from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient

from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.limited_entitlements.domain import LimitedEntitlement
from juya_admin_api.modules.limited_entitlements.repository import (
    InMemoryLimitedEntitlementRepository,
)
from juya_admin_api.modules.limited_entitlements.router import create_limited_entitlement_router
from juya_admin_api.modules.limited_entitlements.service import LimitedEntitlementService
from juya_admin_api.shared.errors import AppError, install_error_handlers

NOW = datetime(2026, 9, 29, tzinfo=UTC)
HEADERS = {"X-Test-Admin": "1", "X-CSRF-Token": "csrf", "X-Idempotency-Key": "original"}


def client_for(status: str) -> tuple[TestClient, InMemoryLimitedEntitlementRepository]:
    repository = InMemoryLimitedEntitlementRepository()
    repository.entitlements["limited-1"] = LimitedEntitlement(
        id="limited-1",
        user_id="user-1",
        campaign_version_id="version-1",
        status=status,
        granted_at=NOW,
        start_deadline=NOW + timedelta(days=7),
        duration_days=3,
        activation_window_days=7,
        scene_ids=("scene-1",),
        activated_at=None if status == "PENDING" else NOW,
        expires_at=None if status == "PENDING" else NOW + timedelta(days=3),
    )
    ticks = 0

    def clock() -> datetime:
        nonlocal ticks
        ticks += 1
        return NOW + timedelta(seconds=ticks)

    async def read_admin(x_test_admin: str | None = Header(default=None)) -> SessionRecord:
        if not x_test_admin:
            raise AppError("ADMIN_SESSION_INVALID", "会话无效", 401)
        return SessionRecord("session", 7, "token", "csrf", "test", NOW, NOW)

    async def write_admin(
        x_test_admin: str | None = Header(default=None),
        x_csrf_token: str | None = Header(default=None),
    ) -> SessionRecord:
        admin = await read_admin(x_test_admin)
        if x_csrf_token != "csrf":
            raise AppError("ADMIN_CSRF_INVALID", "CSRF 无效", 403)
        return admin

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(
        create_limited_entitlement_router(
            LimitedEntitlementService(repository),
            query_repository=repository,
            current_admin=read_admin,
            current_admin_write=write_admin,
            clock=clock,
        )
    )
    return TestClient(app), repository


@pytest.mark.parametrize(
    "operation,status", [("pause", "ACTIVE"), ("resume", "PAUSED"), ("revoke", "PENDING")]
)
def test_state_command_requires_auth_csrf_and_key(operation: str, status: str) -> None:
    client, repository = client_for(status)
    path = f"/api/v1/admin/limited-entitlements/limited-1/commands/{operation}"
    body = {} if operation == "resume" else {"reason": "核对"}
    assert client.post(path, json=body).status_code == 401
    assert client.post(path, json=body, headers={"X-Test-Admin": "1"}).status_code == 403
    assert (
        client.post(
            path, json=body, headers={"X-Test-Admin": "1", "X-CSRF-Token": "csrf"}
        ).status_code
        == 422
    )
    assert repository.entitlements["limited-1"].status == status
    assert repository.entitlements["limited-1"].version == 1


@pytest.mark.parametrize(
    "operation,status", [("pause", "ACTIVE"), ("resume", "PAUSED"), ("revoke", "PENDING")]
)
def test_exact_retry_replays_original_response_without_mutation(
    operation: str, status: str
) -> None:
    client, repository = client_for(status)
    path = f"/api/v1/admin/limited-entitlements/limited-1/commands/{operation}"
    body = {} if operation == "resume" else {"reason": "核对"}
    first = client.post(path, json=body, headers=HEADERS)
    retry = client.post(path, json=body, headers=HEADERS)
    assert first.status_code == 200
    assert retry.status_code == 200
    assert retry.json() == first.json()
    assert repository.entitlements["limited-1"].version == 2


@pytest.mark.parametrize(
    "operation,status,opposite", [("pause", "ACTIVE", "resume"), ("resume", "PAUSED", "pause")]
)
def test_delayed_original_replays_snapshot_after_opposite_command(
    operation: str, status: str, opposite: str
) -> None:
    client, repository = client_for(status)
    base = "/api/v1/admin/limited-entitlements/limited-1/commands"
    body = {} if operation == "resume" else {"reason": "核对"}
    first = client.post(f"{base}/{operation}", json=body, headers=HEADERS)
    assert first.status_code == 200
    changed = client.post(
        f"{base}/{opposite}",
        json={} if opposite == "resume" else {"reason": "核对"},
        headers={**HEADERS, "X-Idempotency-Key": "opposite"},
    )
    assert changed.status_code == 200
    retry = client.post(f"{base}/{operation}", json=body, headers=HEADERS)
    assert retry.status_code == 200
    assert retry.json() == first.json()
    assert repository.entitlements["limited-1"].status == status
    assert repository.entitlements["limited-1"].version == 3


def test_same_key_cannot_change_operation_or_reason() -> None:
    client, repository = client_for("ACTIVE")
    base = "/api/v1/admin/limited-entitlements/limited-1/commands"
    assert client.post(f"{base}/pause", json={"reason": "核对"}, headers=HEADERS).status_code == 200
    for operation, body in [
        ("pause", {"reason": "不同原因"}),
        ("revoke", {"reason": "核对"}),
        ("resume", {}),
    ]:
        response = client.post(f"{base}/{operation}", json=body, headers=HEADERS)
        assert response.status_code == 409
        assert response.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert repository.entitlements["limited-1"].status == "PAUSED"
    assert repository.entitlements["limited-1"].version == 2
