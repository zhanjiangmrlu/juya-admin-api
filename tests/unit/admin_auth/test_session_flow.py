from datetime import UTC, datetime, timedelta

import pytest

from juya_admin_api.modules.admin_auth.domain import (
    AdminUser,
    SessionRecord,
)
from juya_admin_api.modules.admin_auth.service import AdminAuthService, hash_password
from juya_admin_api.shared.errors import AppError


class FakeAuthRepository:
    def __init__(self, user: AdminUser) -> None:
        self.user = user
        self.sessions: dict[str, SessionRecord] = {}
        self.revoked_session_ids: list[str] = []

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

    async def revoke_session(self, session_id: str, now: datetime) -> None:
        self.revoked_session_ids.append(session_id)
        for session in self.sessions.values():
            if session.id == session_id:
                session.revoked_at = now


NOW = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)


def make_service() -> tuple[AdminAuthService, FakeAuthRepository]:
    user = AdminUser(
        id=1,
        public_id="01J00000000000000000000100",
        username="admin",
        password_hash=hash_password("correct horse battery staple"),
    )
    repository = FakeAuthRepository(user)
    return AdminAuthService(repository), repository


@pytest.mark.asyncio
async def test_password_uses_argon2id_and_fifth_failure_locks_account() -> None:
    service, repository = make_service()
    assert repository.user.password_hash.startswith("$argon2id$")

    for _ in range(4):
        with pytest.raises(AppError, match="用户名或密码错误") as exc:
            await service.login_with_password("admin", "wrong", "pytest", NOW)
        assert exc.value.status_code == 401

    with pytest.raises(AppError) as exc:
        await service.login_with_password("admin", "wrong", "pytest", NOW)

    assert exc.value.code == "ADMIN_LOGIN_LOCKED"
    assert repository.user.locked_until == NOW + timedelta(minutes=15)

    with pytest.raises(AppError) as locked:
        await service.login_with_password("admin", "wrong", "pytest", NOW + timedelta(minutes=1))
    assert locked.value.code == "ADMIN_LOGIN_LOCKED"
    assert repository.user.failed_login_count == 5
    assert repository.user.locked_until == NOW + timedelta(minutes=15)


@pytest.mark.asyncio
async def test_password_login_creates_session_without_totp() -> None:
    service, repository = make_service()

    session = await service.login_with_password(
        "admin", "correct horse battery staple", "browser", NOW
    )

    assert session.expires_at == NOW + timedelta(hours=8)
    assert session.csrf_token
    assert len(repository.sessions) == 1


@pytest.mark.asyncio
async def test_csrf_and_logout_revoke_the_authenticated_session() -> None:
    service, repository = make_service()
    session = await service.login_with_password(
        "admin", "correct horse battery staple", "browser", NOW
    )

    authenticated = await service.authenticate_session(session.token, NOW)
    service.verify_csrf(authenticated, session.csrf_token)
    with pytest.raises(AppError) as exc:
        service.verify_csrf(authenticated, "wrong-token")
    assert exc.value.code == "CSRF_INVALID"

    await service.logout(authenticated.id, NOW)

    assert repository.revoked_session_ids == [authenticated.id]
    with pytest.raises(AppError) as exc:
        await service.authenticate_session(session.token, NOW)
    assert exc.value.code == "ADMIN_SESSION_INVALID"


def test_auth_domain_does_not_expose_secret_fields_in_repr() -> None:
    service, repository = make_service()
    assert "password_hash" not in repr(repository.user)
    assert "totp" not in repr(repository.user).lower()
    assert isinstance(service, AdminAuthService)
