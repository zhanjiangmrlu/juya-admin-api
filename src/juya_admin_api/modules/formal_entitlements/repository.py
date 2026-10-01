import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.access_policy.domain import AccessGrant
from juya_admin_api.modules.analytics.events import append_event
from juya_admin_api.modules.formal_entitlements.domain import (
    EntitlementOperation,
    EntitlementTerm,
    FormalEntitlement,
    FormalEntitlementCommand,
    calculate_operation,
    command_hash,
)
from juya_admin_api.modules.limited_entitlements.domain import (
    LimitedEntitlement,
    RemedyMode,
    calculate_pause,
    calculate_remedy,
    calculate_resume,
    calculate_revoke,
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
            await append_event(
                session,
                event_key=f"formal:{updated.id}:{updated.version}",
                event_type="FORMAL_GRANTED" if current is None else "FORMAL_STATUS_CHANGED",
                user_id=user_row.id,
                occurred_at=now,
                dimension=f"package:{command.package_id}",
                payload={"status": updated.status},
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


class SQLAlchemyEntitlementQueryRepository:
    """Read model shared by formal and limited entitlement administration."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_entitlements(
        self, filters: dict[str, str], page: int, page_size: int
    ) -> dict[str, Any]:
        allowed = {"user_id", "type", "status", "package_id", "campaign_id"}
        if set(filters) - allowed or (
            "type" in filters and filters["type"] not in {"FORMAL", "LIMITED"}
        ):
            raise AppError("ENTITLEMENT_FILTER_INVALID", "权益筛选条件不正确", 422)
        if "status" in filters and filters["status"] not in {
            "ACTIVE",
            "PAUSED",
            "REVOKED",
            "PENDING",
            "ENDED",
            "START_EXPIRED",
        }:
            raise AppError("ENTITLEMENT_FILTER_INVALID", "权益状态筛选不正确", 422)
        if page < 1 or not 1 <= page_size <= 100:
            raise AppError("PAGINATION_INVALID", "分页参数不正确", 422)
        query = (
            "SELECT fe.public_id AS id, 'FORMAL' AS type, u.public_id AS user_id, "
            "fe.status, fe.granted_at, fe.expires_at, p.public_id AS package_id, "
            "NULL AS campaign_id FROM formal_entitlement fe "
            "JOIN user_account u ON u.id = fe.user_id "
            "JOIN content_package p ON p.id = fe.package_id "
            "UNION ALL "
            "SELECT le.public_id AS id, 'LIMITED' AS type, u.public_id AS user_id, "
            "le.status, le.granted_at, le.expires_at, NULL AS package_id, "
            "c.public_id AS campaign_id FROM limited_entitlement le "
            "JOIN user_account u ON u.id = le.user_id "
            "JOIN limited_campaign_version cv ON cv.id = le.campaign_version_id "
            "JOIN limited_campaign c ON c.id = cv.campaign_id"
        )
        conditions: list[str] = []
        params: dict[str, Any] = {"limit": page_size, "offset": (page - 1) * page_size}
        for key in allowed:
            if key in filters:
                conditions.append(f"v.{key} = :{key}")
                params[key] = filters[key]
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        async with self._session_factory() as session:
            total = await session.scalar(text(f"SELECT COUNT(*) FROM ({query}) v{where}"), params)
            rows = (
                (
                    await session.execute(
                        text(
                            f"SELECT * FROM ({query}) v{where} "
                            "ORDER BY v.granted_at DESC, v.id DESC "
                            "LIMIT :limit OFFSET :offset"
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
        items = []
        for row in rows:
            item = dict(row)
            item["granted_at"] = _utc(item["granted_at"])
            item["expires_at"] = _utc(item["expires_at"])
            items.append(item)
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total or 0,
        }

    async def get_formal(self, entitlement_id: str) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT fe.public_id AS id, u.public_id AS user_id, "
                            "p.public_id AS package_id, p.name AS package_name, "
                            "fe.status, fe.term, fe.granted_at, fe.expires_at, "
                            "fe.version FROM formal_entitlement fe "
                            "JOIN user_account u ON u.id = fe.user_id "
                            "JOIN content_package p ON p.id = fe.package_id "
                            "WHERE fe.public_id = :id"
                        ),
                        {"id": entitlement_id},
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        result = dict(row)
        result["granted_at"] = _utc(result["granted_at"])
        result["expires_at"] = _utc(result["expires_at"])
        granted_at = _utc(result["granted_at"])
        assert granted_at is not None
        current = FormalEntitlement(
            id=result["id"],
            user_id=result["user_id"],
            package_id=result["package_id"],
            status=result["status"],
            term=EntitlementTerm(result["term"]),
            granted_at=granted_at,
            expires_at=_utc(result["expires_at"]),
            version=result["version"],
        )
        operations: list[str] = []
        now = datetime.now(UTC)
        for operation in (
            EntitlementOperation.RENEW,
            EntitlementOperation.PAUSE,
            EntitlementOperation.RESUME,
            EntitlementOperation.REVOKE,
        ):
            try:
                calculate_operation(
                    current,
                    FormalEntitlementCommand(
                        current.user_id,
                        current.package_id,
                        operation,
                        EntitlementTerm.MONTH_1
                        if operation is EntitlementOperation.RENEW
                        else None,
                    ),
                    now,
                )
            except AppError:
                continue
            operations.append(operation.value)
        result["available_operations"] = operations
        return result

    async def get_limited(self, entitlement_id: str) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT le.public_id AS id, u.public_id AS user_id, "
                            "cv.public_id AS campaign_version_id, c.public_id AS campaign_id, "
                            "c.name AS campaign_name, le.status, le.granted_at, le.start_deadline, "
                            "le.activated_at, le.expires_at, le.remedy_count, le.version, "
                            "cv.duration_days, cv.activation_window_days "
                            "FROM limited_entitlement le "
                            "JOIN user_account u ON u.id = le.user_id "
                            "JOIN limited_campaign_version cv ON cv.id = le.campaign_version_id "
                            "JOIN limited_campaign c ON c.id = cv.campaign_id "
                            "WHERE le.public_id = :id"
                        ),
                        {"id": entitlement_id},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            scenes: list[str] = list(
                (
                    await session.execute(
                        text(
                            "SELECT s.public_id FROM limited_campaign_scene cs "
                            "JOIN scene s ON s.id = cs.scene_id "
                            "JOIN limited_campaign_version cv ON cv.id = cs.campaign_version_id "
                            "WHERE cv.public_id = :version_id ORDER BY cs.position"
                        ),
                        {"version_id": row["campaign_version_id"]},
                    )
                )
                .scalars()
                .all()
            )
        result = {**dict(row), "scene_ids": list(scenes)}
        for field in ("granted_at", "start_deadline", "activated_at", "expires_at"):
            result[field] = _utc(result[field])
        granted_at = _utc(result["granted_at"])
        start_deadline = _utc(result["start_deadline"])
        assert granted_at is not None and start_deadline is not None
        current = LimitedEntitlement(
            id=result["id"],
            user_id=result["user_id"],
            campaign_version_id=result["campaign_version_id"],
            status=result["status"],
            granted_at=granted_at,
            start_deadline=start_deadline,
            duration_days=result["duration_days"],
            activation_window_days=result["activation_window_days"],
            scene_ids=tuple(scenes),
            activated_at=_utc(result["activated_at"]),
            expires_at=_utc(result["expires_at"]),
            remedy_count=result["remedy_count"],
            version=result["version"],
        )
        operations: list[str] = []
        now = datetime.now(UTC)
        calculators: tuple[tuple[str, Callable[[], LimitedEntitlement]], ...] = (
            (
                "EXTEND_START_DEADLINE",
                lambda: calculate_remedy(current, RemedyMode.EXTEND_START_DEADLINE, now),
            ),
            (
                "RESTORE_START_WINDOW",
                lambda: calculate_remedy(current, RemedyMode.RESTORE_START_WINDOW, now),
            ),
            ("PAUSE", lambda: calculate_pause(current, now)),
            ("RESUME", lambda: calculate_resume(current, now)),
            ("REVOKE", lambda: calculate_revoke(current)),
        )
        for name, calculator in calculators:
            try:
                calculator()
            except AppError:
                continue
            operations.append(name)
        result["available_operations"] = operations
        return result

    async def list_packages(self, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        if page < 1 or not 1 <= page_size <= 100:
            raise AppError("PAGINATION_INVALID", "分页参数不正确", 422)
        async with self._session_factory() as session:
            total = await session.scalar(
                text("SELECT COUNT(*) FROM content_package WHERE status = 'ACTIVE'")
            )
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT public_id AS id, name, status, sort_order "
                            "FROM content_package WHERE status = 'ACTIVE' "
                            "ORDER BY sort_order, id LIMIT :limit OFFSET :offset"
                        ),
                        {"limit": page_size, "offset": (page - 1) * page_size},
                    )
                )
                .mappings()
                .all()
            )
        return {
            "items": [dict(row) for row in rows],
            "page": page,
            "page_size": page_size,
            "total": total or 0,
        }


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
