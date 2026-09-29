import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.access_policy.domain import AccessGrant
from juya_admin_api.modules.campaigns.domain import CampaignVersion
from juya_admin_api.modules.limited_entitlements.domain import LimitedEntitlement
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid

ChangeCalculator = Callable[[LimitedEntitlement], LimitedEntitlement]


class LimitedEntitlementRepository(Protocol):
    async def grant(
        self,
        user_id: str,
        campaign_version_id: str,
        actor_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> LimitedEntitlement: ...

    async def activate_for_scene(
        self, user_id: str, scene_id: str, now: datetime
    ) -> LimitedEntitlement | None: ...

    async def change(
        self,
        entitlement_id: str,
        actor_id: str,
        idempotency_key: str,
        operation: str,
        now: datetime,
        calculator: ChangeCalculator,
        *,
        reason: str | None = None,
    ) -> LimitedEntitlement: ...


class InMemoryLimitedEntitlementRepository:
    def __init__(self) -> None:
        self.campaign_versions: dict[str, CampaignVersion] = {}
        self.entitlements: dict[str, LimitedEntitlement] = {}
        self._by_user_version: dict[tuple[str, str], str] = {}
        self._operations: dict[tuple[str, str], LimitedEntitlement] = {}
        self._campaign_locks: dict[str, asyncio.Lock] = {}
        self._entitlement_locks: dict[str, asyncio.Lock] = {}

    async def grant(
        self,
        user_id: str,
        campaign_version_id: str,
        actor_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> LimitedEntitlement:
        version = self.campaign_versions.get(campaign_version_id)
        if version is None:
            raise AppError("CAMPAIGN_VERSION_NOT_FOUND", "限时活动版本不存在", 404)
        version.validate()
        lock = self._campaign_locks.setdefault(campaign_version_id, asyncio.Lock())
        async with lock:
            replay = self._operations.get((actor_id, idempotency_key))
            if replay is not None:
                return replay
            if version.status != "OPEN":
                raise AppError("CAMPAIGN_NOT_OPEN", "限时活动当前不可开通", 409)
            if (user_id, campaign_version_id) in self._by_user_version:
                raise AppError("LIMITED_ENTITLEMENT_EXISTS", "用户已开通过该活动", 409)
            if version.granted_user_count >= version.capacity:
                raise AppError("CAMPAIGN_CAPACITY_REACHED", "活动名额已满", 409)
            entitlement = LimitedEntitlement(
                id=new_ulid(now),
                user_id=user_id,
                campaign_version_id=campaign_version_id,
                status="PENDING",
                granted_at=now,
                start_deadline=now + timedelta(days=version.activation_window_days),
                duration_days=version.duration_days,
                activation_window_days=version.activation_window_days,
                scene_ids=version.scene_ids,
            )
            version.granted_user_count += 1
            version.locked_at = version.locked_at or now
            self.entitlements[entitlement.id] = entitlement
            self._by_user_version[(user_id, campaign_version_id)] = entitlement.id
            self._operations[(actor_id, idempotency_key)] = entitlement
            return entitlement

    async def activate_for_scene(
        self, user_id: str, scene_id: str, now: datetime
    ) -> LimitedEntitlement | None:
        candidates = [
            entitlement
            for entitlement in self.entitlements.values()
            if entitlement.user_id == user_id and scene_id in entitlement.scene_ids
        ]
        for candidate in candidates:
            lock = self._entitlement_locks.setdefault(candidate.id, asyncio.Lock())
            async with lock:
                entitlement = self.entitlements[candidate.id]
                if entitlement.status == "PENDING":
                    if now >= entitlement.start_deadline:
                        entitlement.status = "START_EXPIRED"
                        entitlement.version += 1
                    else:
                        entitlement.status = "ACTIVE"
                        entitlement.activated_at = now
                        entitlement.expires_at = now + timedelta(days=entitlement.duration_days)
                        entitlement.version += 1
                elif (
                    entitlement.status == "ACTIVE"
                    and entitlement.expires_at is not None
                    and now >= entitlement.expires_at
                ):
                    entitlement.status = "ENDED"
                    entitlement.version += 1
                if entitlement.status == "ACTIVE":
                    return entitlement
        return None

    async def change(
        self,
        entitlement_id: str,
        actor_id: str,
        idempotency_key: str,
        operation: str,
        now: datetime,
        calculator: ChangeCalculator,
        *,
        reason: str | None = None,
    ) -> LimitedEntitlement:
        del operation, now, reason
        entitlement = self.entitlements.get(entitlement_id)
        if entitlement is None:
            raise AppError("LIMITED_ENTITLEMENT_NOT_FOUND", "限时权益不存在", 404)
        lock = self._entitlement_locks.setdefault(entitlement_id, asyncio.Lock())
        async with lock:
            replay = self._operations.get((actor_id, idempotency_key))
            if replay is not None:
                return replay
            updated = calculator(entitlement)
            entitlement.status = updated.status
            entitlement.start_deadline = updated.start_deadline
            entitlement.activated_at = updated.activated_at
            entitlement.expires_at = updated.expires_at
            entitlement.remedy_count = updated.remedy_count
            entitlement.version = updated.version
            self._operations[(actor_id, idempotency_key)] = entitlement
            return entitlement


class SQLAlchemyLimitedEntitlementRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_limited(self, entitlement_id: str) -> dict[str, Any] | None:
        from juya_admin_api.modules.formal_entitlements.repository import (
            SQLAlchemyEntitlementQueryRepository,
        )

        return await SQLAlchemyEntitlementQueryRepository(self._session_factory).get_limited(
            entitlement_id
        )

    async def grant(
        self,
        user_id: str,
        campaign_version_id: str,
        actor_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> LimitedEntitlement:
        request_hash = _request_hash("GRANT", user_id, campaign_version_id, None)
        async with self._session_factory() as session, session.begin():
            version = (
                await session.execute(
                    text(
                        "SELECT id, status, duration_days, activation_window_days, "
                        "capacity, granted_user_count FROM limited_campaign_version "
                        "WHERE public_id = :public_id FOR UPDATE"
                    ),
                    {"public_id": campaign_version_id},
                )
            ).first()
            if version is None:
                raise AppError("CAMPAIGN_VERSION_NOT_FOUND", "限时活动版本不存在", 404)
            replay = await self._find_replay(session, actor_id, idempotency_key, request_hash)
            if replay is not None:
                return replay
            if version.status != "OPEN":
                raise AppError("CAMPAIGN_NOT_OPEN", "限时活动当前不可开通", 409)
            if version.duration_days not in {3, 5}:
                raise AppError("CAMPAIGN_DURATION_INVALID", "限时活动只允许 3 天或 5 天", 422)
            if version.granted_user_count >= version.capacity:
                raise AppError("CAMPAIGN_CAPACITY_REACHED", "活动名额已满", 409)
            user_internal_id = await session.scalar(
                text(
                    "SELECT id FROM user_account WHERE public_id = :public_id AND status = 'ACTIVE'"
                ),
                {"public_id": user_id},
            )
            if user_internal_id is None:
                raise AppError("USER_NOT_ELIGIBLE", "用户不存在或当前不可开通", 409)
            exists = await session.scalar(
                text(
                    "SELECT id FROM limited_entitlement "
                    "WHERE user_id = :user_id AND campaign_version_id = :version_id"
                ),
                {"user_id": user_internal_id, "version_id": version.id},
            )
            if exists is not None:
                raise AppError("LIMITED_ENTITLEMENT_EXISTS", "用户已开通过该活动", 409)
            public_id = new_ulid(now)
            start_deadline = now + timedelta(days=version.activation_window_days)
            await session.execute(
                text(
                    "INSERT INTO limited_entitlement "
                    "(public_id, user_id, campaign_version_id, status, granted_at, "
                    "start_deadline, remedy_count, version, created_at, updated_at) "
                    "VALUES (:public_id, :user_id, :version_id, 'PENDING', :now, "
                    ":start_deadline, 0, 1, :now, :now)"
                ),
                {
                    "public_id": public_id,
                    "user_id": user_internal_id,
                    "version_id": version.id,
                    "now": now,
                    "start_deadline": start_deadline,
                },
            )
            entitlement_id = await session.scalar(
                text("SELECT id FROM limited_entitlement WHERE public_id = :public_id"),
                {"public_id": public_id},
            )
            assert entitlement_id is not None
            await session.execute(
                text(
                    "UPDATE limited_campaign_version SET granted_user_count = "
                    "granted_user_count + 1, locked_at = COALESCE(locked_at, :now) "
                    "WHERE id = :id"
                ),
                {"id": version.id, "now": now},
            )
            result = await self._select_by_internal_id(session, entitlement_id)
            assert result is not None
            await self._insert_operation(
                session,
                result,
                entitlement_id,
                "GRANT",
                actor_id,
                idempotency_key,
                request_hash,
                now,
                None,
                before=None,
            )
            return result

    async def activate_for_scene(
        self, user_id: str, scene_id: str, now: datetime
    ) -> LimitedEntitlement | None:
        async with self._session_factory() as session, session.begin():
            entitlement_id = await session.scalar(
                text(
                    "SELECT le.id FROM limited_entitlement le "
                    "JOIN user_account u ON u.id = le.user_id "
                    "JOIN limited_campaign_scene lcs "
                    "ON lcs.campaign_version_id = le.campaign_version_id "
                    "JOIN scene s ON s.id = lcs.scene_id "
                    "WHERE u.public_id = :user_id AND s.public_id = :scene_id "
                    "AND le.status IN ('PENDING','ACTIVE','PAUSED') "
                    "ORDER BY le.id LIMIT 1"
                ),
                {"user_id": user_id, "scene_id": scene_id},
            )
            if entitlement_id is None:
                return None
            entitlement = await self._select_by_internal_id(
                session, entitlement_id, for_update=True
            )
            assert entitlement is not None
            if entitlement.status == "PENDING":
                if now >= entitlement.start_deadline:
                    entitlement.status = "START_EXPIRED"
                else:
                    entitlement.status = "ACTIVE"
                    entitlement.activated_at = now
                    entitlement.expires_at = now + timedelta(days=entitlement.duration_days)
                entitlement.version += 1
                await self._update(session, entitlement, now)
            elif (
                entitlement.status == "ACTIVE"
                and entitlement.expires_at is not None
                and now >= entitlement.expires_at
            ):
                entitlement.status = "ENDED"
                entitlement.version += 1
                await self._update(session, entitlement, now)
            return entitlement if entitlement.status == "ACTIVE" else None

    async def change(
        self,
        entitlement_id: str,
        actor_id: str,
        idempotency_key: str,
        operation: str,
        now: datetime,
        calculator: ChangeCalculator,
        *,
        reason: str | None = None,
    ) -> LimitedEntitlement:
        request_hash = _request_hash(operation, entitlement_id, reason or "", None)
        async with self._session_factory() as session, session.begin():
            internal_id = await session.scalar(
                text("SELECT id FROM limited_entitlement WHERE public_id = :public_id"),
                {"public_id": entitlement_id},
            )
            if internal_id is None:
                raise AppError("LIMITED_ENTITLEMENT_NOT_FOUND", "限时权益不存在", 404)
            current = await self._select_by_internal_id(session, internal_id, for_update=True)
            assert current is not None
            replay = await self._find_replay(session, actor_id, idempotency_key, request_hash)
            if replay is not None:
                return replay
            updated = calculator(current)
            await self._update(session, updated, now)
            await self._insert_operation(
                session,
                updated,
                internal_id,
                operation,
                actor_id,
                idempotency_key,
                request_hash,
                now,
                reason,
                before=current,
            )
            return updated

    async def _find_replay(
        self,
        session: AsyncSession,
        actor_id: str,
        idempotency_key: str,
        request_hash: str,
    ) -> LimitedEntitlement | None:
        row = (
            await session.execute(
                text(
                    "SELECT request_hash, result_entitlement_id "
                    "FROM limited_entitlement_operation "
                    "WHERE operator_id = :actor_id AND idempotency_key = :key"
                ),
                {"actor_id": actor_id, "key": idempotency_key},
            )
        ).first()
        if row is None:
            return None
        if row.request_hash != request_hash:
            raise AppError("IDEMPOTENCY_KEY_REUSED", "幂等键已用于不同请求", 409)
        result = await self._select_by_internal_id(session, row.result_entitlement_id)
        assert result is not None
        return result

    async def _select_by_internal_id(
        self,
        session: AsyncSession,
        entitlement_id: int,
        *,
        for_update: bool = False,
    ) -> LimitedEntitlement | None:
        suffix = " FOR UPDATE" if for_update else ""
        row = (
            await session.execute(
                text(
                    "SELECT le.*, u.public_id AS user_public_id, "
                    "lcv.public_id AS version_public_id, lcv.duration_days, "
                    "lcv.activation_window_days FROM limited_entitlement le "
                    "JOIN user_account u ON u.id = le.user_id "
                    "JOIN limited_campaign_version lcv ON lcv.id = le.campaign_version_id "
                    "WHERE le.id = :id" + suffix
                ),
                {"id": entitlement_id},
            )
        ).first()
        if row is None:
            return None
        scene_rows = (
            await session.execute(
                text(
                    "SELECT s.public_id FROM limited_campaign_scene lcs "
                    "JOIN scene s ON s.id = lcs.scene_id "
                    "WHERE lcs.campaign_version_id = :version_id ORDER BY lcs.position"
                ),
                {"version_id": row.campaign_version_id},
            )
        ).all()
        return _from_row(row, tuple(item.public_id for item in scene_rows))

    async def _update(
        self, session: AsyncSession, entitlement: LimitedEntitlement, now: datetime
    ) -> None:
        await session.execute(
            text(
                "UPDATE limited_entitlement SET status = :status, "
                "start_deadline = :start_deadline, activated_at = :activated_at, "
                "expires_at = :expires_at, remedy_count = :remedy_count, "
                "version = :version, updated_at = :now WHERE public_id = :public_id"
            ),
            {
                "public_id": entitlement.id,
                "status": entitlement.status,
                "start_deadline": entitlement.start_deadline,
                "activated_at": entitlement.activated_at,
                "expires_at": entitlement.expires_at,
                "remedy_count": entitlement.remedy_count,
                "version": entitlement.version,
                "now": now,
            },
        )

    async def _insert_operation(
        self,
        session: AsyncSession,
        result: LimitedEntitlement,
        internal_id: int,
        operation: str,
        actor_id: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
        reason: str | None,
        *,
        before: LimitedEntitlement | None,
    ) -> None:
        await session.execute(
            text(
                "INSERT INTO limited_entitlement_operation "
                "(public_id, entitlement_id, operation_type, before_summary, "
                "after_summary, operator_id, reason, idempotency_key, request_hash, "
                "result_entitlement_id, created_at) VALUES "
                "(:public_id, :entitlement_id, :operation, :before_summary, "
                ":after_summary, :actor_id, :reason, :key, :request_hash, "
                ":result_entitlement_id, :now)"
            ),
            {
                "public_id": new_ulid(now),
                "entitlement_id": internal_id,
                "operation": operation,
                "before_summary": None if before is None else _json_summary(before),
                "after_summary": _json_summary(result),
                "actor_id": actor_id,
                "reason": reason,
                "key": idempotency_key,
                "request_hash": request_hash,
                "result_entitlement_id": internal_id,
                "now": now,
            },
        )


class SQLAlchemyLimitedGrantPort:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def active_grants(
        self, user_id: str, scene_id: str, now: datetime
    ) -> tuple[AccessGrant, ...]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT le.public_id, le.expires_at FROM limited_entitlement le "
                        "JOIN user_account u ON u.id = le.user_id "
                        "JOIN limited_campaign_scene lcs "
                        "ON lcs.campaign_version_id = le.campaign_version_id "
                        "JOIN scene s ON s.id = lcs.scene_id "
                        "WHERE u.public_id = :user_id AND s.public_id = :scene_id "
                        "AND le.status = 'ACTIVE' AND le.expires_at > :now"
                    ),
                    {"user_id": user_id, "scene_id": scene_id, "now": now},
                )
            ).all()
        return tuple(AccessGrant(f"LIMITED:{row.public_id}", _utc(row.expires_at)) for row in rows)


def _request_hash(operation: str, first: str, second: str, mode: str | None) -> str:
    value = json.dumps([operation, first, second, mode], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _from_row(row: Any, scene_ids: tuple[str, ...]) -> LimitedEntitlement:
    granted_at = _utc(row.granted_at)
    start_deadline = _utc(row.start_deadline)
    assert granted_at is not None and start_deadline is not None
    return LimitedEntitlement(
        id=row.public_id,
        user_id=row.user_public_id,
        campaign_version_id=row.version_public_id,
        status=row.status,
        granted_at=granted_at,
        start_deadline=start_deadline,
        duration_days=row.duration_days,
        activation_window_days=row.activation_window_days,
        scene_ids=scene_ids,
        activated_at=_utc(row.activated_at),
        expires_at=_utc(row.expires_at),
        remedy_count=row.remedy_count,
        version=row.version,
    )


def _json_summary(entitlement: LimitedEntitlement) -> str:
    values = asdict(entitlement)
    for key in ("granted_at", "start_deadline", "activated_at", "expires_at"):
        value = values[key]
        values[key] = None if value is None else value.isoformat()
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))
