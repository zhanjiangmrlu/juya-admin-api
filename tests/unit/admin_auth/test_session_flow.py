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
        # 功能:初始化 FakeAuthRepository 测试替身的预设数据和调用记录。
        # 参数:
        #     self: 当前 FakeAuthRepository 测试替身实例,保存本用例的预设状态或调用记录。
        #     user: 待加入认证仓库的测试管理员用户记录。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.user = user
        self.sessions: dict[str, SessionRecord] = {}
        self.revoked_session_ids: list[str] = []

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
        self.revoked_session_ids.append(session_id)
        for session in self.sessions.values():
            if session.id == session_id:
                session.revoked_at = now


NOW = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)


def make_service() -> tuple[AdminAuthService, FakeAuthRepository]:
    # 功能:创建当前用例所需业务服务及内存仓库。
    # 参数:无。
    # 返回:tuple[AdminAuthService, FakeAuthRepository],由本用例预设的数据或所组装的测试资源构成。
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
    # 功能:验证密码使用 Argon2id 且第五次失败锁定账号。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
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
    # 功能:验证密码登录无需 TOTP 即可建立会话。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    service, repository = make_service()

    session = await service.login_with_password(
        "admin", "correct horse battery staple", "browser", NOW
    )

    assert session.expires_at == NOW + timedelta(hours=8)
    assert session.csrf_token
    assert len(repository.sessions) == 1


@pytest.mark.asyncio
async def test_csrf_and_logout_revoke_the_authenticated_session() -> None:
    # 功能:验证 CSRF 检查及退出撤销当前认证会话。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
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
    # 功能:验证认证领域对象的 repr 不暴露秘密字段。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    service, repository = make_service()
    assert "password_hash" not in repr(repository.user)
    assert "totp" not in repr(repository.user).lower()
    assert isinstance(service, AdminAuthService)
