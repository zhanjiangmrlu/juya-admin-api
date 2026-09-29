from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.admin_auth.domain import AdminUser, SessionRecord


def _utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


class SQLAlchemyAdminAuthRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _user(self, row: Any) -> AdminUser:
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
        await self._session.execute(
            text(
                "UPDATE admin_user SET failed_login_count = 0, locked_until = NULL "
                "WHERE id = :user_id"
            ),
            {"user_id": user_id},
        )

    async def create_session(self, session: SessionRecord) -> None:
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

    async def revoke_session(self, session_id: str, now: datetime) -> None:
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
        self._session_factory = session_factory

    def _repository(self, session: AsyncSession) -> SQLAlchemyAdminAuthRepository:
        return SQLAlchemyAdminAuthRepository(session)

    async def get_user_by_username(self, username: str) -> AdminUser | None:
        async with self._session_factory() as session:
            return await self._repository(session).get_user_by_username(username)

    async def record_password_failure(
        self, user_id: int, failed_count: int, locked_until: datetime | None
    ) -> None:
        async with self._session_factory() as session, session.begin():
            await self._repository(session).record_password_failure(
                user_id, failed_count, locked_until
            )

    async def reset_password_failures(self, user_id: int) -> None:
        async with self._session_factory() as session, session.begin():
            await self._repository(session).reset_password_failures(user_id)

    async def create_session(self, session_record: SessionRecord) -> None:
        async with self._session_factory() as session, session.begin():
            await self._repository(session).create_session(session_record)

    async def get_session_by_token_hash(self, token_hash: str) -> SessionRecord | None:
        async with self._session_factory() as session:
            return await self._repository(session).get_session_by_token_hash(token_hash)

    async def revoke_session(self, session_id: str, now: datetime) -> None:
        async with self._session_factory() as session, session.begin():
            await self._repository(session).revoke_session(session_id, now)
