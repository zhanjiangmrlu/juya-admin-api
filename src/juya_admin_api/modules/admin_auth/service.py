import hashlib
import hmac
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

from juya_admin_api.modules.admin_auth.domain import (
    AdminSession,
    AdminUser,
    SessionRecord,
)
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid

PASSWORD_FAILURE_LIMIT = 5
PASSWORD_LOCK_DURATION = timedelta(minutes=15)
SESSION_DURATION = timedelta(hours=8)
_PASSWORD_HASHER = PasswordHasher(type=Type.ID)
_DUMMY_PASSWORD_HASH = _PASSWORD_HASHER.hash("juya-dummy-password")


class AdminAuthRepository(Protocol):
    async def get_user_by_username(self, username: str) -> AdminUser | None:
        # 功能: 按登录名查找管理员账号.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     username: 管理员登录名.
        # 返回: 管理员账号;查无对应登录名时为 None.
        ...

    async def record_password_failure(
        self, user_id: int, failed_count: int, locked_until: datetime | None
    ) -> None:
        # 功能: 记录密码失败次数及账号锁定截止时间.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 管理员账号数据库数值主键.
        #     failed_count: 累计密码验证失败次数.
        #     locked_until: 密码登录锁定截止时间;None 表示解除锁定.
        # 返回: 无返回值;正常完成表示本次操作成功.
        ...

    async def reset_password_failures(self, user_id: int) -> None:
        # 功能: 清除管理员密码失败计数和锁定状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 管理员账号数据库数值主键.
        # 返回: 无返回值;正常完成表示本次操作成功.
        ...

    async def create_session(self, session: SessionRecord) -> None:
        # 功能: 持久化管理员登录会话记录.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session: 管理员会话记录,包含身份,令牌摘要和有效期.
        # 返回: 无返回值;正常完成表示本次操作成功.
        ...

    async def get_session_by_token_hash(self, token_hash: str) -> SessionRecord | None:
        # 功能: 按会话令牌摘要查找管理员会话.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     token_hash: 管理员会话令牌的 SHA-256 摘要.
        # 返回: 管理员会话记录;不存在时为 None.
        ...

    async def update_session_csrf(self, session_id: str, csrf_hash: str) -> None:
        # 功能: 更新管理员会话保存的 CSRF 令牌摘要.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session_id: 管理员会话公开标识.
        #     csrf_hash: 轮换后的 CSRF 令牌 SHA-256 摘要.
        # 返回: 无返回值;正常完成表示本次操作成功.
        ...

    async def revoke_session(self, session_id: str, now: datetime) -> None:
        # 功能: 标记管理员会话已撤销及撤销时间.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session_id: 管理员会话公开标识.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 无返回值;正常完成表示本次操作成功.
        ...


def hash_password(password: str) -> str:
    # 功能: 使用 Argon2 对管理员密码生成单向摘要.
    # 参数:
    #     password: 管理员提交的密码明文,仅用于验证或生成 Argon2 摘要.
    # 返回: 可用于 Argon2 验证的密码摘要字符串.
    return _PASSWORD_HASHER.hash(password)


def _sha256(value: str) -> str:
    # 功能: 生成文本的 SHA-256 十六进制摘要.
    # 参数:
    #     value: 待计算 SHA-256 摘要的令牌或文本.
    # 返回: 文本对应的 SHA-256 十六进制摘要.
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class AdminAuthService:
    def __init__(
        self,
        repository: AdminAuthRepository,
        *,
        token_factory: Callable[[int], str] = secrets.token_urlsafe,
    ) -> None:
        # 功能: 初始化管理员认证对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     repository: 提供管理员认证持久化和查询能力的仓储.
        #     token_factory: 按指定随机字节数生成会话或 CSRF 令牌的回调.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self._repository = repository
        self._token_factory = token_factory

    async def login_with_password(
        self,
        username: str,
        password: str,
        device_summary: str,
        now: datetime,
    ) -> AdminSession:
        # 功能: 校验密码及账号锁定状态,建立管理员会话.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     username: 管理员登录名.
        #     password: 管理员提交的密码明文,仅用于验证或生成 Argon2 摘要.
        #     device_summary: 管理员登录设备摘要,随会话持久化.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 新建的管理员会话和客户端令牌.
        user = await self._repository.get_user_by_username(username)
        password_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
        verified = False
        try:
            verified = _PASSWORD_HASHER.verify(password_hash, password)
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            verified = False

        if (
            user is not None
            and user.status == "ACTIVE"
            and user.locked_until is not None
            and now < user.locked_until
        ):
            raise AppError("ADMIN_LOGIN_LOCKED", "登录失败次数过多, 请稍后再试", 429)

        if user is None or user.status != "ACTIVE" or not verified:
            if user is not None and user.status == "ACTIVE":
                failed_count = user.failed_login_count + 1
                locked_until = (
                    now + PASSWORD_LOCK_DURATION if failed_count >= PASSWORD_FAILURE_LIMIT else None
                )
                await self._repository.record_password_failure(user.id, failed_count, locked_until)
                if locked_until is not None:
                    raise AppError(
                        "ADMIN_LOGIN_LOCKED",
                        "登录失败次数过多, 请稍后再试",
                        429,
                    )
            raise AppError("INVALID_ADMIN_CREDENTIALS", "用户名或密码错误", 401)

        await self._repository.reset_password_failures(user.id)
        token = self._token_factory(32)
        csrf_token = self._token_factory(32)
        session = SessionRecord(
            id=new_ulid(now),
            admin_user_id=user.id,
            token_hash=_sha256(token),
            csrf_hash=_sha256(csrf_token),
            device_summary=device_summary[:200],
            expires_at=now + SESSION_DURATION,
            created_at=now,
        )
        await self._repository.create_session(session)
        return AdminSession(
            id=session.id,
            token=token,
            csrf_token=csrf_token,
            expires_at=session.expires_at,
        )

    async def authenticate_session(self, session_token: str, now: datetime) -> SessionRecord:
        # 功能: 校验会话令牌,撤销状态和有效期.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session_token: 浏览器 Cookie 中的管理员会话令牌明文.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 经过查询或认证的管理员会话记录.
        session = await self._repository.get_session_by_token_hash(_sha256(session_token))
        if session is None or session.revoked_at is not None or now >= session.expires_at:
            raise AppError("ADMIN_SESSION_INVALID", "管理员会话无效或已过期", 401)
        return session

    def verify_csrf(self, session: SessionRecord, csrf_token: str | None) -> None:
        # 功能: 比对会话 CSRF 摘要,拒绝缺失或不匹配的令牌.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session: 管理员会话记录,包含身份,令牌摘要和有效期.
        #     csrf_token: 客户端提交的 CSRF 令牌明文,缺失或与会话不匹配时拒绝写入.
        # 返回: 无返回值;正常完成表示本次操作成功.
        supplied_hash = _sha256(csrf_token or "")
        if not hmac.compare_digest(session.csrf_hash, supplied_hash):
            raise AppError("CSRF_INVALID", "CSRF 校验失败", 403)

    async def rotate_csrf(self, session: SessionRecord) -> str:
        # 功能: 生成新的 CSRF 令牌并更新会话保存的摘要.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session: 管理员会话记录,包含身份,令牌摘要和有效期.
        # 返回: 新生成的 CSRF 令牌明文,供浏览器后续写操作提交.
        csrf_token = self._token_factory(32)
        csrf_hash = _sha256(csrf_token)
        await self._repository.update_session_csrf(session.id, csrf_hash)
        session.csrf_hash = csrf_hash
        return csrf_token

    async def logout(self, session_id: str, now: datetime) -> None:
        # 功能: 撤销当前管理员会话.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session_id: 管理员会话公开标识.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 无返回值;正常完成表示本次操作成功.
        await self._repository.revoke_session(session_id, now)
