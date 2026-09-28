from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from juya_admin_api.modules.admin_auth.domain import AdminUser, AuthChallenge, SessionRecord


def _utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


class SQLAlchemyAdminAuthRepository:
    def __init__(
        self,
        session: AsyncSession,
        *,
        decrypt_totp_secret: Callable[[bytes], str],
    ) -> None:
        self._session = session
        self._decrypt_totp_secret = decrypt_totp_secret

    def _user(self, row: Any) -> AdminUser:
        values = row._mapping
        return AdminUser(
            id=values["id"],
            public_id=values["public_id"],
            username=values["username"],
            password_hash=values["password_hash"],
            totp_secret=self._decrypt_totp_secret(values["totp_secret_ciphertext"]),
            status=values["status"],
            failed_login_count=values["failed_login_count"],
            locked_until=_utc(values["locked_until"]),
            last_totp_step=values["last_totp_step"],
        )

    async def get_user_by_username(self, username: str) -> AdminUser | None:
        row = (
            await self._session.execute(
                text("SELECT * FROM admin_user WHERE username = :username"),
                {"username": username},
            )
        ).first()
        return self._user(row) if row is not None else None

    async def get_user_by_id(self, user_id: int) -> AdminUser | None:
        row = (
            await self._session.execute(
                text("SELECT * FROM admin_user WHERE id = :user_id"),
                {"user_id": user_id},
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

    async def create_challenge(self, challenge: AuthChallenge) -> None:
        await self._session.execute(
            text(
                "INSERT INTO admin_auth_challenge "
                "(id, admin_user_id, client_ip_hash, expires_at, created_at) "
                "VALUES (:id, :admin_user_id, :client_ip_hash, :expires_at, UTC_TIMESTAMP(6))"
            ),
            {
                "id": challenge.id,
                "admin_user_id": challenge.admin_user_id,
                "client_ip_hash": challenge.client_ip_hash,
                "expires_at": challenge.expires_at,
            },
        )

    async def get_challenge(self, challenge_id: str) -> AuthChallenge | None:
        row = (
            await self._session.execute(
                text("SELECT * FROM admin_auth_challenge WHERE id = :id"),
                {"id": challenge_id},
            )
        ).first()
        if row is None:
            return None
        values = row._mapping
        expires_at = _utc(values["expires_at"])
        assert expires_at is not None
        return AuthChallenge(
            id=values["id"],
            admin_user_id=values["admin_user_id"],
            client_ip_hash=values["client_ip_hash"],
            expires_at=expires_at,
            consumed_at=_utc(values["consumed_at"]),
        )

    async def consume_challenge(
        self,
        challenge_id: str,
        user_id: int,
        totp_step: int,
        now: datetime,
    ) -> bool:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                text(
                    "UPDATE admin_user AS u JOIN admin_auth_challenge AS c "
                    "ON c.admin_user_id = u.id "
                    "SET c.consumed_at = :now, u.last_totp_step = :totp_step "
                    "WHERE c.id = :challenge_id AND u.id = :user_id "
                    "AND c.consumed_at IS NULL AND c.expires_at > :now "
                    "AND (u.last_totp_step IS NULL OR u.last_totp_step < :totp_step)"
                ),
                {
                    "now": now,
                    "totp_step": totp_step,
                    "challenge_id": challenge_id,
                    "user_id": user_id,
                },
            ),
        )
        return result.rowcount == 1

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
