import hashlib
import hmac
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

from juya_admin_api.modules.admin_auth.domain import (
    AdminSession,
    AdminUser,
    AuthChallenge,
    SessionRecord,
    TotpChallenge,
)
from juya_admin_api.shared.errors import AppError

PASSWORD_FAILURE_LIMIT = 5
PASSWORD_LOCK_DURATION = timedelta(minutes=15)
CHALLENGE_DURATION = timedelta(minutes=5)
SESSION_DURATION = timedelta(hours=8)
_CROCKFORD32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_PASSWORD_HASHER = PasswordHasher(type=Type.ID)
_DUMMY_PASSWORD_HASH = _PASSWORD_HASHER.hash("juya-dummy-password")


class AdminAuthRepository(Protocol):
    async def get_user_by_username(self, username: str) -> AdminUser | None: ...

    async def get_user_by_id(self, user_id: int) -> AdminUser | None: ...

    async def record_password_failure(
        self, user_id: int, failed_count: int, locked_until: datetime | None
    ) -> None: ...

    async def reset_password_failures(self, user_id: int) -> None: ...

    async def create_challenge(self, challenge: AuthChallenge) -> None: ...

    async def get_challenge(self, challenge_id: str) -> AuthChallenge | None: ...

    async def consume_challenge(
        self,
        challenge_id: str,
        user_id: int,
        totp_step: int,
        now: datetime,
    ) -> bool: ...

    async def create_session(self, session: SessionRecord) -> None: ...

    async def get_session_by_token_hash(self, token_hash: str) -> SessionRecord | None: ...

    async def revoke_session(self, session_id: str, now: datetime) -> None: ...


def hash_password(password: str) -> str:
    return _PASSWORD_HASHER.hash(password)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _encode_base32(value: int, length: int) -> str:
    chars = ["0"] * length
    for index in range(length - 1, -1, -1):
        chars[index] = _CROCKFORD32[value & 31]
        value >>= 5
    return "".join(chars)


def new_ulid(now: datetime) -> str:
    timestamp_ms = int(now.timestamp() * 1000)
    return _encode_base32(timestamp_ms, 10) + _encode_base32(secrets.randbits(80), 16)


class AdminAuthService:
    def __init__(
        self,
        repository: AdminAuthRepository,
        *,
        token_factory: Callable[[int], str] = secrets.token_urlsafe,
    ) -> None:
        self._repository = repository
        self._token_factory = token_factory

    async def verify_password(
        self,
        username: str,
        password: str,
        client_ip: str,
        now: datetime,
    ) -> TotpChallenge:
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
        challenge = AuthChallenge(
            id=new_ulid(now),
            admin_user_id=user.id,
            client_ip_hash=_sha256(client_ip),
            expires_at=now + CHALLENGE_DURATION,
        )
        await self._repository.create_challenge(challenge)
        return TotpChallenge(id=challenge.id, expires_at=challenge.expires_at)

    async def verify_totp_and_create_session(
        self,
        challenge_id: str,
        code: str,
        device_summary: str,
        now: datetime,
    ) -> AdminSession:
        challenge = await self._repository.get_challenge(challenge_id)
        if challenge is None or challenge.consumed_at is not None or now >= challenge.expires_at:
            raise AppError("TOTP_CHALLENGE_INVALID", "验证码会话无效或已过期", 401)

        user = await self._repository.get_user_by_id(challenge.admin_user_id)
        if user is None or user.status != "ACTIVE":
            raise AppError("TOTP_CHALLENGE_INVALID", "验证码会话无效或已过期", 401)

        totp = pyotp.TOTP(user.totp_secret)
        if not totp.verify(code, for_time=now, valid_window=0):
            raise AppError("TOTP_INVALID", "动态验证码错误", 401)
        totp_step = int(now.timestamp()) // int(totp.interval)
        consumed = await self._repository.consume_challenge(challenge.id, user.id, totp_step, now)
        if not consumed:
            raise AppError("TOTP_ALREADY_USED", "动态验证码已使用", 409)

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
        session = await self._repository.get_session_by_token_hash(_sha256(session_token))
        if session is None or session.revoked_at is not None or now >= session.expires_at:
            raise AppError("ADMIN_SESSION_INVALID", "管理员会话无效或已过期", 401)
        return session

    def verify_csrf(self, session: SessionRecord, csrf_token: str | None) -> None:
        supplied_hash = _sha256(csrf_token or "")
        if not hmac.compare_digest(session.csrf_hash, supplied_hash):
            raise AppError("CSRF_INVALID", "CSRF 校验失败", 403)

    async def logout(self, session_id: str, now: datetime) -> None:
        await self._repository.revoke_session(session_id, now)
