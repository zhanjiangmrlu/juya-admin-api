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
    # 功能:组装当前用例所需路由、依赖和内存仓库的 HTTP 测试客户端。
    # 参数:
    #     status: 本测试预设的业务状态或返回状态,用于检查状态约束。
    # 返回:tuple[TestClient, InMemoryLimitedEntitlementRepository],由本用例预设的数据或所组装的
    #       测试资源构成。
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
        # 功能:提供可控测试时间,避免真实时钟影响重试与期限断言。
        # 参数:无。
        # 返回:当前预设或按调用次数推进的测试时间。
        nonlocal ticks
        ticks += 1
        return NOW + timedelta(seconds=ticks)

    async def read_admin(x_test_admin: str | None = Header(default=None)) -> SessionRecord:
        # 功能:检查测试认证头并返回预设管理员会话。
        # 参数:
        #     x_test_admin: 测试专用管理员认证请求头,用于替代真实登录会话。
        # 返回:测试管理员会话。
        if not x_test_admin:
            raise AppError("ADMIN_SESSION_INVALID", "会话无效", 401)
        return SessionRecord("session", 7, "token", "csrf", "test", NOW, NOW)

    async def write_admin(
        x_test_admin: str | None = Header(default=None),
        x_csrf_token: str | None = Header(default=None),
    ) -> SessionRecord:
        # 功能:在测试认证通过后检查写请求 CSRF 令牌。
        # 参数:
        #     x_test_admin: 测试专用管理员认证请求头,用于替代真实登录会话。
        #     x_csrf_token: 写操作请求的 CSRF 令牌,与当前测试会话的预设值比较。
        # 返回:测试管理员会话。
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
    # 功能:验证状态命令要求认证、CSRF 和幂等键。
    # 参数:
    #     operation: 待执行的业务命令名称,如开通、暂停、恢复或撤销。
    #     status: 本测试预设的业务状态或返回状态,用于检查状态约束。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
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
    # 功能:验证完全相同的重试返回原响应且不修改状态。
    # 参数:
    #     operation: 待执行的业务命令名称,如开通、暂停、恢复或撤销。
    #     status: 本测试预设的业务状态或返回状态,用于检查状态约束。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
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
    # 功能:验证相反命令后延迟到达的原请求仍重放原快照。
    # 参数:
    #     operation: 待执行的业务命令名称,如开通、暂停、恢复或撤销。
    #     status: 本测试预设的业务状态或返回状态,用于检查状态约束。
    #     opposite: 首次命令之后执行的相反操作,用于验证延迟重放。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
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
    # 功能:验证同一幂等键不能更改操作或原因。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
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
