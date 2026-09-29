"""HTTP contracts for the admin entitlement and campaign management surface."""

import importlib
import importlib.util
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Header
from fastapi.testclient import TestClient

from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.shared.errors import AppError, install_error_handlers

NOW = datetime(2026, 9, 29, tzinfo=UTC)


class CampaignRepository:
    def __init__(self) -> None:
        self.version = 1
        self.status = "DRAFT"
        self.capacity = 3
        self.audit: list[str] = []
        self.fail_audit_once = False
        self.responses: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}

    def replay(self, kwargs: dict[str, Any]) -> dict[str, Any] | None:
        identity = (kwargs["actor_id"], kwargs["idempotency_key"])
        existing = self.responses.get(identity)
        if existing is None:
            return None
        if existing[0] != kwargs["request_hash"]:
            raise AppError("IDEMPOTENCY_KEY_REUSED", "幂等键已用于不同请求", 409)
        return existing[1]

    def complete(self, kwargs: dict[str, Any], result: dict[str, Any]) -> None:
        self.responses[(kwargs["actor_id"], kwargs["idempotency_key"])] = (
            kwargs["request_hash"],
            result,
        )

    def detail(self) -> dict[str, Any]:
        return {
            "id": "campaign-1",
            "name": "Launch",
            "status": self.status,
            "version": self.version,
            "created_at": NOW,
            "updated_at": NOW,
            "current_version": {
                "id": "version-1",
                "version_no": 1,
                "status": self.status,
                "duration_days": 3,
                "activation_window_days": 7,
                "capacity": self.capacity,
                "granted_user_count": 1,
                "grant_starts_at": None,
                "grant_ends_at": None,
                "locked_at": None,
                "version": self.version,
                "scene_ids": [],
            },
        }

    async def list(self, filters: dict[str, str], page: int, page_size: int) -> dict[str, Any]:
        if filters.get("status") not in (None, self.status):
            return {"items": [], "page": page, "page_size": page_size, "total": 0}
        item = self.detail().copy()
        item.pop("current_version")
        return {"items": [item], "page": page, "page_size": page_size, "total": 1}

    async def get(self, campaign_id: str) -> dict[str, Any] | None:
        return self.detail() if campaign_id == "campaign-1" else None

    async def save(self, campaign_id: str | None, **kwargs: Any) -> dict[str, Any]:
        replay = self.replay(kwargs)
        if replay is not None:
            return replay
        if campaign_id is not None and kwargs["expected_version"] != self.version:
            raise AppError(
                "CAMPAIGN_VERSION_CONFLICT",
                "活动版本已变化",
                409,
                {"current_version": self.version},
            )
        self.version += 1
        self.audit.append("SAVE")
        result = self.detail()
        self.complete(kwargs, result)
        return result

    async def command(self, campaign_id: str, operation: str, **kwargs: Any) -> dict[str, Any]:
        replay = self.replay(kwargs)
        if replay is not None:
            return replay
        if self.fail_audit_once:
            self.fail_audit_once = False
            raise AppError("AUDIT_WRITE_FAILED", "审计写入失败", 503)
        if kwargs["expected_version"] != self.version:
            raise AppError(
                "CAMPAIGN_VERSION_CONFLICT",
                "活动版本已变化",
                409,
                {"current_version": self.version},
            )
        if operation == "open" and self.status != "DRAFT":
            raise AppError("CAMPAIGN_STATE_CONFLICT", "当前活动状态不可执行此操作", 409)
        if operation == "capacity" and kwargs.get("capacity", self.capacity) < 1:
            raise AppError("CAMPAIGN_CAPACITY_BELOW_GRANTED", "容量不能低于累计开通人数", 409)
        if operation == "open":
            self.status = "OPEN"
        if operation == "capacity":
            self.capacity = kwargs["capacity"]
        self.version += 1
        self.audit.append(operation.upper())
        result = self.detail()
        self.complete(kwargs, result)
        return result


def _client() -> tuple[TestClient, CampaignRepository]:
    assert importlib.util.find_spec("juya_admin_api.modules.campaigns.router") is not None
    campaign_router = importlib.import_module("juya_admin_api.modules.campaigns.router")
    campaign_service = importlib.import_module("juya_admin_api.modules.campaigns.service")

    async def read_admin(x_test_admin: str | None = Header(default=None)) -> SessionRecord:
        if x_test_admin is None:
            raise AppError("ADMIN_SESSION_INVALID", "管理员会话无效或已过期", 401)
        return SessionRecord("session", 7, "token", "csrf", "test device", NOW, NOW)

    async def write_admin(
        x_test_admin: str | None = Header(default=None),
        x_csrf_token: str | None = Header(default=None),
    ) -> SessionRecord:
        admin = await read_admin(x_test_admin)
        if x_csrf_token != "csrf":
            raise AppError("ADMIN_CSRF_INVALID", "CSRF token 无效", 403)
        return admin

    repository = CampaignRepository()
    service = campaign_service.CampaignService(repository)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(
        campaign_router.create_campaign_router(
            service, current_admin=read_admin, current_admin_write=write_admin, clock=lambda: NOW
        )
    )
    return TestClient(app), repository


def test_campaign_reads_require_session_and_return_page() -> None:
    client, _ = _client()
    assert client.get("/api/v1/admin/campaigns").status_code == 401
    response = client.get(
        "/api/v1/admin/campaigns?page=1&page_size=10", headers={"X-Test-Admin": "1"}
    )
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["id"] == "campaign-1"
    assert (
        client.get("/api/v1/admin/campaigns?status=BAD", headers={"X-Test-Admin": "1"}).status_code
        == 422
    )


def test_campaign_command_needs_csrf_and_idempotency_replays_without_new_audit() -> None:
    client, repository = _client()
    path = "/api/v1/admin/campaigns/campaign-1/commands/open"
    body = {"expected_version": 1}
    assert client.post(path, json=body, headers={"X-Test-Admin": "1"}).status_code == 403
    headers = {"X-Test-Admin": "1", "X-CSRF-Token": "csrf"}
    assert client.post(path, json=body, headers=headers).status_code == 422
    headers["X-Idempotency-Key"] = "open-once"
    first = client.post(path, json=body, headers=headers)
    assert first.status_code == 200
    assert first.json()["status"] == "OPEN"
    assert client.post(path, json=body, headers=headers).json() == first.json()
    assert repository.audit == ["OPEN"]
    changed = client.post(path, json={"expected_version": 2}, headers=headers)
    assert changed.status_code == 409
    assert changed.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert repository.audit == ["OPEN"]


def test_campaign_conflicts_leave_state_and_audit_unchanged() -> None:
    client, repository = _client()
    headers = {"X-Test-Admin": "1", "X-CSRF-Token": "csrf", "X-Idempotency-Key": "capacity-1"}
    path = "/api/v1/admin/campaigns/campaign-1/commands/capacity"
    failed = client.post(path, json={"expected_version": 1, "capacity": 0}, headers=headers)
    assert failed.status_code == 409
    assert failed.json()["code"] == "CAMPAIGN_CAPACITY_BELOW_GRANTED"
    assert repository.version == 1
    assert repository.audit == []
    headers["X-Idempotency-Key"] = "stale-1"
    stale = client.post(path, json={"expected_version": 2, "capacity": 5}, headers=headers)
    assert stale.status_code == 409
    assert stale.json()["details"]["current_version"] == 1
    assert repository.audit == []


def test_campaign_audit_failure_can_retry_same_key_without_double_write() -> None:
    client, repository = _client()
    repository.fail_audit_once = True
    headers = {
        "X-Test-Admin": "1",
        "X-CSRF-Token": "csrf",
        "X-Idempotency-Key": "audit-retry-1",
    }
    path = "/api/v1/admin/campaigns/campaign-1/commands/open"
    failed = client.post(path, json={"expected_version": 1}, headers=headers)
    assert failed.status_code == 503
    assert repository.audit == []
    assert repository.version == 1
    retry = client.post(path, json={"expected_version": 1}, headers=headers)
    assert retry.status_code == 200
    assert retry.json()["status"] == "OPEN"
    assert repository.audit == ["OPEN"]


def test_entitlement_and_package_reads_use_authenticated_pages() -> None:
    from juya_admin_api.modules.formal_entitlements.router import create_entitlement_query_router

    class QueryRepository:
        async def list_entitlements(
            self, filters: dict[str, str], page: int, page_size: int
        ) -> dict[str, Any]:
            assert filters == {"type": "FORMAL"}
            return {
                "items": [
                    {
                        "id": "ent-1",
                        "type": "FORMAL",
                        "user_id": "user-1",
                        "status": "ACTIVE",
                        "granted_at": NOW,
                        "expires_at": None,
                        "package_id": "package-1",
                        "campaign_id": None,
                    }
                ],
                "page": page,
                "page_size": page_size,
                "total": 1,
            }

        async def list_packages(self, page: int, page_size: int) -> dict[str, Any]:
            return {
                "items": [
                    {"id": "package-1", "name": "Starter", "status": "ACTIVE", "sort_order": 1}
                ],
                "page": page,
                "page_size": page_size,
                "total": 1,
            }

    async def admin(x_test_admin: str | None = Header(default=None)) -> SessionRecord:
        if x_test_admin is None:
            raise AppError("ADMIN_SESSION_INVALID", "管理员会话无效或已过期", 401)
        return SessionRecord("session", 7, "token", "csrf", "test device", NOW, NOW)

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(create_entitlement_query_router(QueryRepository(), current_admin=admin))
    client = TestClient(app)
    assert client.get("/api/v1/admin/entitlements?type=FORMAL").status_code == 401
    headers = {"X-Test-Admin": "1"}
    page = client.get("/api/v1/admin/entitlements?type=FORMAL", headers=headers)
    assert page.status_code == 200
    assert page.json()["items"][0]["id"] == "ent-1"
    packages = client.get("/api/v1/admin/content-packages", headers=headers)
    assert packages.json()["total"] == 1
    assert client.get("/api/v1/admin/entitlements?type=BAD", headers=headers).status_code == 422
