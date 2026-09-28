import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.access_policy.domain import AccessGrant
from juya_admin_api.modules.formal_entitlements.domain import (
    EntitlementTerm,
    FormalEntitlement,
    FormalEntitlementCommand,
    command_hash,
)
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid

OperationCalculator = Callable[
    [FormalEntitlement | None, FormalEntitlementCommand, datetime],
    FormalEntitlement,
]


class FormalEntitlementRepository(Protocol):
    async def get(self, user_id: str, package_id: str) -> FormalEntitlement | None: ...

    async def apply(
        self,
        command: FormalEntitlementCommand,
        actor: str,
        idempotency_key: str,
        now: datetime,
        calculator: OperationCalculator,
    ) -> FormalEntitlement: ...


class InMemoryFormalEntitlementRepository:
    def __init__(self) -> None:
        self.entitlements: dict[tuple[str, str], FormalEntitlement] = {}
        self.operations: dict[tuple[str, str], tuple[str, FormalEntitlement]] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def get(self, user_id: str, package_id: str) -> FormalEntitlement | None:
        return self.entitlements.get((user_id, package_id))

    async def apply(
        self,
        command: FormalEntitlementCommand,
        actor: str,
        idempotency_key: str,
        now: datetime,
        calculator: OperationCalculator,
    ) -> FormalEntitlement:
        identity = (command.user_id, command.package_id)
        lock = self._locks.setdefault(identity, asyncio.Lock())
        async with lock:
            request_hash = command_hash(command)
            existing_operation = self.operations.get((actor, idempotency_key))
            if existing_operation is not None:
                if existing_operation[0] != request_hash:
                    raise AppError(
                        "IDEMPOTENCY_KEY_REUSED",
                        "幂等键已用于不同请求",
                        409,
                    )
                return existing_operation[1]
            updated = calculator(self.entitlements.get(identity), command, now)
            self.entitlements[identity] = updated
            self.operations[(actor, idempotency_key)] = (request_hash, updated)
            return updated


def _utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


class SQLAlchemyFormalEntitlementRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, user_id: str, package_id: str) -> FormalEntitlement | None:
        async with self._session_factory() as session:
            return await self._select_entitlement(session, user_id, package_id)

    async def apply(
        self,
        command: FormalEntitlementCommand,
        actor: str,
        idempotency_key: str,
        now: datetime,
        calculator: OperationCalculator,
    ) -> FormalEntitlement:
        async with self._session_factory() as session, session.begin():
            user_row = (
                await session.execute(
                    text(
                        "SELECT id FROM user_account WHERE public_id = :public_id "
                        "AND status = 'ACTIVE' FOR UPDATE"
                    ),
                    {"public_id": command.user_id},
                )
            ).first()
            if user_row is None:
                raise AppError("USER_NOT_ELIGIBLE", "用户不存在或当前不可授予权益", 409)

            request_hash = command_hash(command)
            replay = (
                await session.execute(
                    text(
                        "SELECT request_hash, result_entitlement_id "
                        "FROM formal_entitlement_operation "
                        "WHERE operator_id = :actor AND idempotency_key = :key"
                    ),
                    {"actor": actor, "key": idempotency_key},
                )
            ).first()
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise AppError(
                        "IDEMPOTENCY_KEY_REUSED",
                        "幂等键已用于不同请求",
                        409,
                    )
                result = await self._select_by_internal_id(session, replay.result_entitlement_id)
                assert result is not None
                return result

            package_row = (
                await session.execute(
                    text(
                        "SELECT id FROM content_package WHERE public_id = :public_id "
                        "AND status = 'ACTIVE'"
                    ),
                    {"public_id": command.package_id},
                )
            ).first()
            if package_row is None:
                raise AppError("CONTENT_PACKAGE_NOT_FOUND", "内容包不存在或不可用", 404)

            current = await self._select_entitlement(
                session, command.user_id, command.package_id, for_update=True
            )
            updated = calculator(current, command, now)
            before_summary = None if current is None else _summary(current)
            if current is None:
                await session.execute(
                    text(
                        "INSERT INTO formal_entitlement "
                        "(public_id, user_id, package_id, term, status, granted_at, "
                        "expires_at, version, created_at, updated_at) "
                        "VALUES (:public_id, :user_id, :package_id, :term, :status, "
                        ":granted_at, :expires_at, :version, :now, :now)"
                    ),
                    {
                        "public_id": updated.id,
                        "user_id": user_row.id,
                        "package_id": package_row.id,
                        "term": updated.term.value,
                        "status": updated.status,
                        "granted_at": updated.granted_at,
                        "expires_at": updated.expires_at,
                        "version": updated.version,
                        "now": now,
                    },
                )
            else:
                await session.execute(
                    text(
                        "UPDATE formal_entitlement SET term = :term, status = :status, "
                        "granted_at = :granted_at, expires_at = :expires_at, "
                        "version = :version, updated_at = :now WHERE public_id = :public_id"
                    ),
                    {
                        "public_id": updated.id,
                        "term": updated.term.value,
                        "status": updated.status,
                        "granted_at": updated.granted_at,
                        "expires_at": updated.expires_at,
                        "version": updated.version,
                        "now": now,
                    },
                )
            entitlement_id = await session.scalar(
                text("SELECT id FROM formal_entitlement WHERE public_id = :public_id"),
                {"public_id": updated.id},
            )
            assert entitlement_id is not None
            await session.execute(
                text(
                    "INSERT INTO formal_entitlement_operation "
                    "(public_id, entitlement_id, operation_type, term, before_summary, "
                    "after_summary, operator_id, reason, idempotency_key, request_hash, "
                    "result_entitlement_id, created_at) VALUES "
                    "(:public_id, :entitlement_id, :operation_type, :term, "
                    ":before_summary, :after_summary, :operator_id, :reason, "
                    ":idempotency_key, :request_hash, :result_entitlement_id, :now)"
                ),
                {
                    "public_id": new_ulid(now),
                    "entitlement_id": entitlement_id,
                    "operation_type": command.operation.value,
                    "term": None if command.term is None else command.term.value,
                    "before_summary": (
                        None
                        if before_summary is None
                        else json.dumps(before_summary, separators=(",", ":"))
                    ),
                    "after_summary": json.dumps(_summary(updated), separators=(",", ":")),
                    "operator_id": actor,
                    "reason": command.reason,
                    "idempotency_key": idempotency_key,
                    "request_hash": request_hash,
                    "result_entitlement_id": entitlement_id,
                    "now": now,
                },
            )
            return updated

    async def _select_entitlement(
        self,
        session: AsyncSession,
        user_id: str,
        package_id: str,
        *,
        for_update: bool = False,
    ) -> FormalEntitlement | None:
        suffix = " FOR UPDATE" if for_update else ""
        row = (
            await session.execute(
                text(
                    "SELECT fe.public_id, u.public_id AS user_public_id, "
                    "p.public_id AS package_public_id, fe.status, fe.term, "
                    "fe.granted_at, fe.expires_at, fe.version "
                    "FROM formal_entitlement fe "
                    "JOIN user_account u ON u.id = fe.user_id "
                    "JOIN content_package p ON p.id = fe.package_id "
                    "WHERE u.public_id = :user_id AND p.public_id = :package_id" + suffix
                ),
                {"user_id": user_id, "package_id": package_id},
            )
        ).first()
        return None if row is None else _from_row(row)

    async def _select_by_internal_id(
        self, session: AsyncSession, entitlement_id: int
    ) -> FormalEntitlement | None:
        row = (
            await session.execute(
                text(
                    "SELECT fe.public_id, u.public_id AS user_public_id, "
                    "p.public_id AS package_public_id, fe.status, fe.term, "
                    "fe.granted_at, fe.expires_at, fe.version "
                    "FROM formal_entitlement fe "
                    "JOIN user_account u ON u.id = fe.user_id "
                    "JOIN content_package p ON p.id = fe.package_id WHERE fe.id = :id"
                ),
                {"id": entitlement_id},
            )
        ).first()
        return None if row is None else _from_row(row)


class SQLAlchemyFormalGrantPort:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def active_grants(
        self, user_id: str, scene_id: str, now: datetime
    ) -> tuple[AccessGrant, ...]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT fe.public_id, fe.expires_at FROM formal_entitlement fe "
                        "JOIN user_account u ON u.id = fe.user_id "
                        "JOIN content_package_scene cps ON cps.package_id = fe.package_id "
                        "JOIN scene s ON s.id = cps.scene_id "
                        "WHERE u.public_id = :user_id AND s.public_id = :scene_id "
                        "AND fe.status = 'ACTIVE' "
                        "AND (fe.expires_at IS NULL OR fe.expires_at > :now)"
                    ),
                    {"user_id": user_id, "scene_id": scene_id, "now": now},
                )
            ).all()
        return tuple(AccessGrant(f"FORMAL:{row.public_id}", _utc(row.expires_at)) for row in rows)


def _from_row(row: Any) -> FormalEntitlement:
    granted_at = _utc(row.granted_at)
    assert granted_at is not None
    return FormalEntitlement(
        id=row.public_id,
        user_id=row.user_public_id,
        package_id=row.package_public_id,
        status=row.status,
        term=EntitlementTerm(row.term),
        granted_at=granted_at,
        expires_at=_utc(row.expires_at),
        version=row.version,
    )


def _summary(entitlement: FormalEntitlement) -> dict[str, object]:
    values = asdict(entitlement)
    values["term"] = entitlement.term.value
    for key in ("granted_at", "expires_at"):
        value = values[key]
        values[key] = None if value is None else value.isoformat()
    return values
