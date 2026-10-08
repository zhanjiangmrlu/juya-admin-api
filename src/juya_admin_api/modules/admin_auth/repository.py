from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.admin_auth.domain import AdminUser, SessionRecord


def _utc(value: datetime | None) -> datetime | None:
    # 功能: 将数据库无时区时间补为 UTC 并保留空值.
    # 参数:
    #     value: 待规范化时区或转换业务日期的时间;None 保留为空.
    # 返回: 规范化日期时间;输入为空或允许空值时为 None.
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


class SQLAlchemyAdminAuthRepository:
    def __init__(self, session: AsyncSession) -> None:
        # 功能: 初始化管理员认证对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session: 当前 SQLAlchemy 异步数据库会话,在调用方事务内执行读写.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self._session = session

    def _user(self, row: Any) -> AdminUser:
        # 功能: 把管理员数据库记录转换为认证领域对象.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     row: 查询得到的管理员认证数据库记录.
        # 返回: 管理员账号及其密码摘要,状态和锁定信息.
        values = row._mapping
        return AdminUser(
            id=values["id"],
            public_id=values["public_id"],
            username=values["username"],
            password_hash=values["password_hash"],
            status=values["status"],
            failed_login_count=values["failed_login_count"],
            locked_until=_utc(values["locked_until"]),
        )

    async def get_user_by_username(self, username: str) -> AdminUser | None:
        # 功能: 按登录名查找管理员账号.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     username: 管理员登录名.
        # 返回: 管理员账号;查无对应登录名时为 None.
        row = (
            await self._session.execute(
                text("SELECT * FROM admin_user WHERE username = :username"),
                {"username": username},
            )
        ).first()
        return self._user(row) if row is not None else None

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
        await self._session.execute(
            text(
                "UPDATE admin_user SET failed_login_count = :failed_count, "
                "locked_until = :locked_until WHERE id = :user_id"
            ),
            {
                "user_id": user_id,
                "failed_count": failed_count,
                "locked_until": locked_until,
            },
        )

    async def reset_password_failures(self, user_id: int) -> None:
        # 功能: 清除管理员密码失败计数和锁定状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 管理员账号数据库数值主键.
        # 返回: 无返回值;正常完成表示本次操作成功.
        await self._session.execute(
            text(
                "UPDATE admin_user SET failed_login_count = 0, locked_until = NULL "
                "WHERE id = :user_id"
            ),
            {"user_id": user_id},
        )

    async def create_session(self, session: SessionRecord) -> None:
        # 功能: 持久化管理员登录会话记录.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session: 管理员会话记录,包含身份,令牌摘要和有效期.
        # 返回: 无返回值;正常完成表示本次操作成功.
        await self._session.execute(
            text(
                "INSERT INTO admin_session "
                "(id, admin_user_id, token_hash, csrf_hash, device_summary, expires_at, "
                "created_at) VALUES (:id, :admin_user_id, :token_hash, :csrf_hash, "
                ":device_summary, :expires_at, :created_at)"
            ),
            {
                "id": session.id,
                "admin_user_id": session.admin_user_id,
                "token_hash": session.token_hash,
                "csrf_hash": session.csrf_hash,
                "device_summary": session.device_summary,
                "expires_at": session.expires_at,
                "created_at": session.created_at,
            },
        )

    async def get_session_by_token_hash(self, token_hash: str) -> SessionRecord | None:
        # 功能: 按会话令牌摘要查找管理员会话.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     token_hash: 管理员会话令牌的 SHA-256 摘要.
        # 返回: 管理员会话记录;不存在时为 None.
        row = (
            await self._session.execute(
                text("SELECT * FROM admin_session WHERE token_hash = :token_hash"),
                {"token_hash": token_hash},
            )
        ).first()
        if row is None:
            return None
        values = row._mapping
        expires_at = _utc(values["expires_at"])
        created_at = _utc(values["created_at"])
        assert expires_at is not None and created_at is not None
        return SessionRecord(
            id=values["id"],
            admin_user_id=values["admin_user_id"],
            token_hash=values["token_hash"],
            csrf_hash=values["csrf_hash"],
            device_summary=values["device_summary"],
            expires_at=expires_at,
            created_at=created_at,
            revoked_at=_utc(values["revoked_at"]),
        )

    async def update_session_csrf(self, session_id: str, csrf_hash: str) -> None:
        # 功能: 更新管理员会话保存的 CSRF 令牌摘要.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session_id: 管理员会话公开标识.
        #     csrf_hash: 轮换后的 CSRF 令牌 SHA-256 摘要.
        # 返回: 无返回值;正常完成表示本次操作成功.
        await self._session.execute(
            text("UPDATE admin_session SET csrf_hash = :csrf_hash WHERE id = :session_id"),
            {"session_id": session_id, "csrf_hash": csrf_hash},
        )

    async def revoke_session(self, session_id: str, now: datetime) -> None:
        # 功能: 标记管理员会话已撤销及撤销时间.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session_id: 管理员会话公开标识.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 无返回值;正常完成表示本次操作成功.
        await self._session.execute(
            text(
                "UPDATE admin_session SET revoked_at = COALESCE(revoked_at, :now) "
                "WHERE id = :session_id"
            ),
            {"session_id": session_id, "now": now},
        )


class TransactionalAdminAuthRepository:
    """Request-safe repository facade that owns short database transactions."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        # 功能: 初始化管理员认证对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session_factory: 创建 SQLAlchemy 异步会话的工厂,每次操作独立管理事务.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self._session_factory = session_factory

    def _repository(self, session: AsyncSession) -> SQLAlchemyAdminAuthRepository:
        # 功能: 为给定事务会话构建管理员认证仓储.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session: 当前 SQLAlchemy 异步数据库会话,在调用方事务内执行读写.
        # 返回: 绑定指定异步会话的管理员认证仓储.
        return SQLAlchemyAdminAuthRepository(session)

    async def get_user_by_username(self, username: str) -> AdminUser | None:
        # 功能: 按登录名查找管理员账号.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     username: 管理员登录名.
        # 返回: 管理员账号;查无对应登录名时为 None.
        async with self._session_factory() as session:
            return await self._repository(session).get_user_by_username(username)

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
        async with self._session_factory() as session, session.begin():
            await self._repository(session).record_password_failure(
                user_id, failed_count, locked_until
            )

    async def reset_password_failures(self, user_id: int) -> None:
        # 功能: 清除管理员密码失败计数和锁定状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 管理员账号数据库数值主键.
        # 返回: 无返回值;正常完成表示本次操作成功.
        async with self._session_factory() as session, session.begin():
            await self._repository(session).reset_password_failures(user_id)

    async def create_session(self, session_record: SessionRecord) -> None:
        # 功能: 持久化管理员登录会话记录.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session_record: 待持久化的管理员会话记录.
        # 返回: 无返回值;正常完成表示本次操作成功.
        async with self._session_factory() as session, session.begin():
            await self._repository(session).create_session(session_record)

    async def get_session_by_token_hash(self, token_hash: str) -> SessionRecord | None:
        # 功能: 按会话令牌摘要查找管理员会话.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     token_hash: 管理员会话令牌的 SHA-256 摘要.
        # 返回: 管理员会话记录;不存在时为 None.
        async with self._session_factory() as session:
            return await self._repository(session).get_session_by_token_hash(token_hash)

    async def update_session_csrf(self, session_id: str, csrf_hash: str) -> None:
        # 功能: 更新管理员会话保存的 CSRF 令牌摘要.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session_id: 管理员会话公开标识.
        #     csrf_hash: 轮换后的 CSRF 令牌 SHA-256 摘要.
        # 返回: 无返回值;正常完成表示本次操作成功.
        async with self._session_factory() as session, session.begin():
            await self._repository(session).update_session_csrf(session_id, csrf_hash)

    async def revoke_session(self, session_id: str, now: datetime) -> None:
        # 功能: 标记管理员会话已撤销及撤销时间.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session_id: 管理员会话公开标识.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 无返回值;正常完成表示本次操作成功.
        async with self._session_factory() as session, session.begin():
            await self._repository(session).revoke_session(session_id, now)
