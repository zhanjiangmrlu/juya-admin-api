import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from juya_admin_api.modules.admin_auth.domain import (
    AdminUser,
    SessionRecord,
)
from juya_admin_api.modules.admin_auth.router import create_admin_security_router
from juya_admin_api.modules.admin_auth.service import AdminAuthService, hash_password
from juya_admin_api.modules.audit.service import AuditService
from juya_admin_api.modules.system_config.service import (
    InMemorySystemConfigRepository,
    SystemConfigService,
)
from juya_admin_api.shared.errors import AppError, install_error_handlers
from juya_admin_api.shared.idempotency import (
    IdempotencyService,
    InMemoryIdempotencyRepository,
)

NOW = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
PROJECT_ROOT = Path(__file__).parents[2]


class FakeAuthRepository:
    def __init__(self) -> None:
        # 功能:初始化 FakeAuthRepository 测试替身的预设数据和调用记录。
        # 参数:
        #     self: 当前 FakeAuthRepository 测试替身实例,保存本用例的预设状态或调用记录。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.user = AdminUser(
            id=1,
            public_id="01J00000000000000000000100",
            username="admin",
            password_hash=hash_password("secret-password"),
        )
        self.sessions: dict[str, SessionRecord] = {}

    async def get_user_by_username(self, username: str) -> AdminUser | None:
        # 功能:按登录名从认证替身仓库返回预设管理员。
        # 参数:
        #     self: 当前 FakeAuthRepository 测试替身实例,保存本用例的预设状态或调用记录。
        #     username: 测试管理员登录名,用于查询认证账户。
        # 返回:AdminUser | None,由本用例预设的数据或所组装的测试资源构成。
        return self.user if username == self.user.username else None

    async def record_password_failure(
        self, user_id: int, failed_count: int, locked_until: datetime | None
    ) -> None:
        # 功能:更新预设管理员的密码失败次数及锁定截止时间。
        # 参数:
        #     self: 当前 FakeAuthRepository 测试替身实例,保存本用例的预设状态或调用记录。
        #     user_id: 目标用户标识;认证仓库中使用管理员数据库主键。
        #     failed_count: 管理员累计密码登录失败次数。
        #     locked_until: 账号锁定截止时间;None 表示不锁定。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.user.failed_login_count = failed_count
        self.user.locked_until = locked_until

    async def reset_password_failures(self, user_id: int) -> None:
        # 功能:清除预设管理员的密码失败计数及锁定状态。
        # 参数:
        #     self: 当前 FakeAuthRepository 测试替身实例,保存本用例的预设状态或调用记录。
        #     user_id: 目标用户标识;认证仓库中使用管理员数据库主键。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.user.failed_login_count = 0
        self.user.locked_until = None

    async def create_session(self, session: SessionRecord) -> None:
        # 功能:按令牌哈希将管理员会话保存到内存测试仓库。
        # 参数:
        #     self: 当前 FakeAuthRepository 测试替身实例,保存本用例的预设状态或调用记录。
        #     session: 待保存或使用的管理员会话记录。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.sessions[session.token_hash] = session

    async def get_session_by_token_hash(self, token_hash: str) -> SessionRecord | None:
        # 功能:按访问令牌哈希查询内存测试会话。
        # 参数:
        #     self: 当前 FakeAuthRepository 测试替身实例,保存本用例的预设状态或调用记录。
        #     token_hash: 会话访问令牌的哈希,作为测试仓库查询键。
        # 返回:SessionRecord | None,由本用例预设的数据或所组装的测试资源构成。
        return self.sessions.get(token_hash)

    async def update_session_csrf(self, session_id: str, csrf_hash: str) -> None:
        # 功能:更新内存会话的 CSRF 哈希。
        # 参数:
        #     self: 当前 FakeAuthRepository 测试替身实例,保存本用例的预设状态或调用记录。
        #     session_id: 待更新或撤销的管理员会话标识。
        #     csrf_hash: 更新后的 CSRF 令牌哈希。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        for session in self.sessions.values():
            if session.id == session_id:
                session.csrf_hash = csrf_hash

    async def revoke_session(self, session_id: str, now: datetime) -> None:
        # 功能:从内存仓库撤销指定管理员会话。
        # 参数:
        #     self: 当前 FakeAuthRepository 测试替身实例,保存本用例的预设状态或调用记录。
        #     session_id: 待更新或撤销的管理员会话标识。
        #     now: 测试指定的当前时间,用于稳定计算期限、状态迁移和事件时间。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        for session in self.sessions.values():
            if session.id == session_id:
                session.revoked_at = now


def make_client(
    *, session_cookie_secure: bool = True
) -> tuple[TestClient, InMemorySystemConfigRepository]:
    # 功能:组装当前用例所需服务、错误处理器及路由的测试客户端。
    # 参数:无。
    # 返回:tuple[TestClient, InMemorySystemConfigRepository],由本用例预设的数据或所组装的测试资
    #       源构成。
    auth_service = AdminAuthService(FakeAuthRepository())
    config_repository = InMemorySystemConfigRepository({"feedback_sla_hours": ({"value": 24}, 1)})
    app = FastAPI()
    install_error_handlers(app)
    # 匿名函数: 注入固定测试时间或 UTC 当前时间, 控制接口和签名的时间源。
    # 参数: 无。
    # 返回: 对应测试时间或 UTC 当前时间。
    app.include_router(
        create_admin_security_router(
            auth_service,
            SystemConfigService(config_repository),
            clock=lambda: NOW,
            session_cookie_secure=session_cookie_secure,
        )
    )
    scheme = "https" if session_cookie_secure else "http"
    return TestClient(app, base_url=f"{scheme}://testserver"), config_repository


def test_http_test_session_preserves_csrf_and_logout() -> None:
    # 验证 HTTP 测试登录保留会话恢复、CSRF 校验及退出语义
    client, _ = make_client(session_cookie_secure=False)
    response = client.post(
        "/api/v1/admin/session",
        json={"username": "admin", "password": "secret-password"},
    )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "secure" not in cookie
    assert "httponly" in cookie and "samesite=strict" in cookie
    restored = client.get("/api/v1/admin/session")
    assert restored.status_code == 200
    assert client.post("/api/v1/admin/session/logout").status_code == 403
    csrf = restored.json()["csrf_token"]
    logout = client.post("/api/v1/admin/session/logout", headers={"X-CSRF-Token": csrf})
    assert logout.status_code == 204
    assert "secure" not in logout.headers["set-cookie"].lower()
    assert client.get("/api/v1/admin/session").status_code == 401


def login(client: TestClient) -> str:
    # 功能:通过测试客户端登录并检查安全 Cookie,返回 CSRF 令牌。
    # 参数:
    #     client: FastAPI TestClient,供发起登录及业务 HTTP 请求。
    # 返回:已登录测试会话的 CSRF 令牌。
    response = client.post(
        "/api/v1/admin/session",
        json={"username": "admin", "password": "secret-password"},
    )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=strict" in cookie
    return response.json()["csrf_token"]


def test_totp_login_endpoint_is_not_exposed() -> None:
    # 功能:验证 TOTP 登录接口未对外暴露。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    client, _ = make_client()

    response = client.post(
        "/api/v1/admin/session/totp",
        json={"challenge_id": "0" * 26, "code": "123456", "device_summary": "pytest"},
    )

    assert response.status_code == 404


def test_admin_write_requires_csrf_and_logout_clears_cookie() -> None:
    # 功能:验证管理员写操作要求 CSRF 且退出时清除 Cookie。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    client, _ = make_client()
    csrf_token = login(client)

    missing = client.post("/api/v1/admin/session/logout")
    assert missing.status_code == 403
    assert missing.json()["code"] == "CSRF_INVALID"

    response = client.post("/api/v1/admin/session/logout", headers={"X-CSRF-Token": csrf_token})
    assert response.status_code == 204
    assert "max-age=0" in response.headers["set-cookie"].lower()


def test_authenticated_session_can_be_restored_after_page_reload() -> None:
    # 功能:验证页面刷新后可以恢复已认证会话。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    client, _ = make_client()
    csrf_token = login(client)

    response = client.get("/api/v1/admin/session")

    assert response.status_code == 200
    restored_csrf_token = response.json()["csrf_token"]
    assert restored_csrf_token != csrf_token
    assert response.json()["expires_at"] == "2026-09-28T18:00:00Z"

    old_token = client.patch(
        "/api/v1/admin/settings/feedback_sla_hours",
        headers={"X-CSRF-Token": csrf_token},
        json={"value": {"value": 48}, "expected_version": 1},
    )
    assert old_token.status_code == 403

    restored_token = client.patch(
        "/api/v1/admin/settings/feedback_sla_hours",
        headers={"X-CSRF-Token": restored_csrf_token},
        json={"value": {"value": 48}, "expected_version": 1},
    )
    assert restored_token.status_code == 200


def test_settings_update_rejects_stale_version() -> None:
    # 功能:验证配置更新拒绝过期版本。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    client, _ = make_client()
    csrf_token = login(client)
    headers = {"X-CSRF-Token": csrf_token}

    updated = client.patch(
        "/api/v1/admin/settings/feedback_sla_hours",
        headers=headers,
        json={"value": {"value": 48}, "expected_version": 1},
    )
    assert updated.status_code == 200
    assert updated.json()["version"] == 2

    stale = client.patch(
        "/api/v1/admin/settings/feedback_sla_hours",
        headers=headers,
        json={"value": {"value": 72}, "expected_version": 1},
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "CONFIG_VERSION_CONFLICT"


@pytest.mark.asyncio
async def test_idempotency_replays_same_request_and_rejects_key_reuse() -> None:
    # 功能:验证幂等请求可重放且拒绝同一键承载不同请求。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    service = IdempotencyService(InMemoryIdempotencyRepository())
    first = await service.begin("grant", "admin-1", "key-1", "hash-a")
    await service.complete(first, {"status": "ok"}, status_code=201)

    replay = await service.begin("grant", "admin-1", "key-1", "hash-a")
    assert replay.is_replay is True
    assert replay.response_body == {"status": "ok"}
    assert replay.response_status == 201

    with pytest.raises(AppError) as exc:
        await service.begin("grant", "admin-1", "key-1", "hash-b")
    assert exc.value.code == "IDEMPOTENCY_KEY_REUSED"


def test_audit_summary_uses_explicit_allowlist() -> None:
    # 功能:验证审计摘要使用明确的字段白名单。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    summary = AuditService.summarize(
        {
            "status": "ACTIVE",
            "wechat_id": "must-not-leak",
            "token": "must-not-leak",
        },
        allowed_fields=frozenset({"status"}),
    )

    assert summary == {"status": "ACTIVE"}


@pytest.fixture(scope="module")
def mysql_connection() -> Connection:
    # 功能:准备完成迁移的 MySQL 连接并在用例结束后释放。
    # 参数:无。
    # 返回:测试资源生成器;产生数据库连接或会话后,在退出时释放资源。
    database_url = os.getenv("JUYA_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("JUYA_TEST_DATABASE_URL is required for MySQL integration tests")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    connection = engine.connect()
    yield connection
    connection.close()
    engine.dispose()


def test_admin_schema_enforces_identity_and_idempotency_uniqueness(
    mysql_connection: Connection,
) -> None:
    # 功能:验证管理员数据库约束保证身份及幂等键唯一。
    # 参数:
    #     mysql_connection: 已执行迁移的 MySQL 测试连接,供实际 SQL 断言使用。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    with mysql_connection.begin():
        mysql_connection.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in (
            "admin_session",
            "admin_auth_challenge",
            "admin_user",
            "idempotency_record",
        ):
            mysql_connection.execute(text(f"DELETE FROM {table}"))
        mysql_connection.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        mysql_connection.execute(
            text(
                "INSERT INTO admin_user "
                "(public_id, username, password_hash, totp_secret_ciphertext, status) "
                "VALUES ('01J00000000000000000000100', 'admin', 'hash', "
                "X'736563726574', 'ACTIVE')"
            )
        )
        mysql_connection.execute(
            text(
                "INSERT INTO idempotency_record "
                "(scope, actor_id, idempotency_key, request_hash) "
                "VALUES ('grant', 'admin-1', 'key-1', :request_hash)"
            ),
            {"request_hash": "a" * 64},
        )

    with pytest.raises(IntegrityError), mysql_connection.begin():
        mysql_connection.execute(
            text(
                "INSERT INTO admin_user "
                "(public_id, username, password_hash, totp_secret_ciphertext, status) "
                "VALUES ('01J00000000000000000000101', 'admin', 'hash', "
                "X'736563726574', 'ACTIVE')"
            )
        )

    with pytest.raises(IntegrityError), mysql_connection.begin():
        mysql_connection.execute(
            text(
                "INSERT INTO idempotency_record "
                "(scope, actor_id, idempotency_key, request_hash) "
                "VALUES ('grant', 'admin-1', 'key-1', :request_hash)"
            ),
            {"request_hash": "b" * 64},
        )

    version = mysql_connection.scalar(text("SELECT version FROM schema_version WHERE id = 1"))
    assert version >= 2
