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
        self.user = AdminUser(
            id=1,
            public_id="01J00000000000000000000100",
            username="admin",
            password_hash=hash_password("secret-password"),
        )
        self.sessions: dict[str, SessionRecord] = {}

    async def get_user_by_username(self, username: str) -> AdminUser | None:
        return self.user if username == self.user.username else None

    async def record_password_failure(
        self, user_id: int, failed_count: int, locked_until: datetime | None
    ) -> None:
        self.user.failed_login_count = failed_count
        self.user.locked_until = locked_until

    async def reset_password_failures(self, user_id: int) -> None:
        self.user.failed_login_count = 0
        self.user.locked_until = None

    async def create_session(self, session: SessionRecord) -> None:
        self.sessions[session.token_hash] = session

    async def get_session_by_token_hash(self, token_hash: str) -> SessionRecord | None:
        return self.sessions.get(token_hash)

    async def update_session_csrf(self, session_id: str, csrf_hash: str) -> None:
        for session in self.sessions.values():
            if session.id == session_id:
                session.csrf_hash = csrf_hash

    async def revoke_session(self, session_id: str, now: datetime) -> None:
        for session in self.sessions.values():
            if session.id == session_id:
                session.revoked_at = now


def make_client() -> tuple[TestClient, InMemorySystemConfigRepository]:
    auth_service = AdminAuthService(FakeAuthRepository())
    config_repository = InMemorySystemConfigRepository({"feedback_sla_hours": ({"value": 24}, 1)})
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(
        create_admin_security_router(
            auth_service,
            SystemConfigService(config_repository),
            clock=lambda: NOW,
        )
    )
    return TestClient(app, base_url="https://testserver"), config_repository


def login(client: TestClient) -> str:
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
    client, _ = make_client()

    response = client.post(
        "/api/v1/admin/session/totp",
        json={"challenge_id": "0" * 26, "code": "123456", "device_summary": "pytest"},
    )

    assert response.status_code == 404


def test_admin_write_requires_csrf_and_logout_clears_cookie() -> None:
    client, _ = make_client()
    csrf_token = login(client)

    missing = client.post("/api/v1/admin/session/logout")
    assert missing.status_code == 403
    assert missing.json()["code"] == "CSRF_INVALID"

    response = client.post("/api/v1/admin/session/logout", headers={"X-CSRF-Token": csrf_token})
    assert response.status_code == 204
    assert "max-age=0" in response.headers["set-cookie"].lower()


def test_authenticated_session_can_be_restored_after_page_reload() -> None:
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
