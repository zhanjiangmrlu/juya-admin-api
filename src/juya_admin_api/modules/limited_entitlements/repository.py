import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.access_policy.domain import AccessGrant
from juya_admin_api.modules.analytics.lifecycle import limited_event
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
    ) -> LimitedEntitlement:
        # 功能: 按指定活动版本授予限时权益并控制重复开通和容量.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     campaign_version_id: 限时活动版本公开标识,权益绑定该版本的固定场景集合.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 计算或持久化后的限时权益状态.
        ...

    async def activate_for_scene(
        self, user_id: str, scene_id: str, now: datetime
    ) -> LimitedEntitlement | None:
        # 功能: 首次访问活动场景时激活对应限时权益.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     scene_id: 学习场景公开标识.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 匹配的限时权益;无可匹配权益时为 None.
        ...

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
        # 功能: 锁定限时权益并执行状态变更,保存幂等和审计信息.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     entitlement_id: 待查询或变更的权益标识.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     operation: 待执行的业务命令,例如授予,暂停,恢复或撤销.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        #     calculator: 根据当前权益,操作命令和时间计算新权益的回调.
        #     reason: 业务状态变更的原因说明,供校验和审计记录.
        # 返回: 计算或持久化后的限时权益状态.
        ...


class InMemoryLimitedEntitlementRepository:
    def __init__(self) -> None:
        # 功能: 初始化限时权益对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self.campaign_versions: dict[str, CampaignVersion] = {}
        self.entitlements: dict[str, LimitedEntitlement] = {}
        self._by_user_version: dict[tuple[str, str], str] = {}
        self._operations: dict[tuple[str, str], tuple[str, LimitedEntitlement]] = {}
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
        # 功能: 按指定活动版本授予限时权益并控制重复开通和容量.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     campaign_version_id: 限时活动版本公开标识,权益绑定该版本的固定场景集合.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 计算或持久化后的限时权益状态.
        request_hash = _request_hash("GRANT", user_id, campaign_version_id, None)
        version = self.campaign_versions.get(campaign_version_id)
        if version is None:
            raise AppError("CAMPAIGN_VERSION_NOT_FOUND", "限时活动版本不存在", 404)
        version.validate()
        lock = self._campaign_locks.setdefault(campaign_version_id, asyncio.Lock())
        async with lock:
            replay = self._find_replay(actor_id, idempotency_key, request_hash)
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
            self._operations[(actor_id, idempotency_key)] = (request_hash, replace(entitlement))
            return entitlement

    async def activate_for_scene(
        self, user_id: str, scene_id: str, now: datetime
    ) -> LimitedEntitlement | None:
        # 功能: 首次访问活动场景时激活对应限时权益.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     scene_id: 学习场景公开标识.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 匹配的限时权益;无可匹配权益时为 None.
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
        # 功能: 锁定限时权益并执行状态变更,保存幂等和审计信息.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     entitlement_id: 待查询或变更的权益标识.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     operation: 待执行的业务命令,例如授予,暂停,恢复或撤销.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        #     calculator: 根据当前权益,操作命令和时间计算新权益的回调.
        #     reason: 业务状态变更的原因说明,供校验和审计记录.
        # 返回: 计算或持久化后的限时权益状态.
        request_hash = _request_hash(operation, entitlement_id, reason or "", None)
        entitlement = self.entitlements.get(entitlement_id)
        if entitlement is None:
            raise AppError("LIMITED_ENTITLEMENT_NOT_FOUND", "限时权益不存在", 404)
        lock = self._entitlement_locks.setdefault(entitlement_id, asyncio.Lock())
        async with lock:
            replay = self._find_replay(actor_id, idempotency_key, request_hash)
            if replay is not None:
                return replay
            updated = calculator(entitlement)
            entitlement.status = updated.status
            entitlement.start_deadline = updated.start_deadline
            entitlement.activated_at = updated.activated_at
            entitlement.expires_at = updated.expires_at
            entitlement.remedy_count = updated.remedy_count
            entitlement.version = updated.version
            self._operations[(actor_id, idempotency_key)] = (request_hash, replace(entitlement))
            return entitlement

    def _find_replay(
        self, actor_id: str, idempotency_key: str, request_hash: str
    ) -> LimitedEntitlement | None:
        # 功能: 检查同一主体和幂等键的历史执行结果及请求一致性.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     request_hash: 规范化业务请求的摘要,用于检测同一幂等键被不同请求复用.
        # 返回: 匹配的限时权益;无可匹配权益时为 None.
        stored = self._operations.get((actor_id, idempotency_key))
        if stored is None:
            return None
        if stored[0] != request_hash:
            raise AppError("IDEMPOTENCY_KEY_REUSED", "幂等键已用于不同请求", 409)
        return replace(stored[1])


class SQLAlchemyLimitedEntitlementRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        # 功能: 初始化限时权益对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session_factory: 创建 SQLAlchemy 异步会话的工厂,每次操作独立管理事务.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self._session_factory = session_factory

    async def get_limited(self, entitlement_id: str) -> dict[str, Any] | None:
        # 功能: 查询限时权益及关联活动和学习统计.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     entitlement_id: 待查询或变更的权益标识.
        # 返回: 限时权益及关联活动信息,固定场景标识和可用操作.无匹配权益时为 None.
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
        # 功能: 按指定活动版本授予限时权益并控制重复开通和容量.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     campaign_version_id: 限时活动版本公开标识,权益绑定该版本的固定场景集合.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 计算或持久化后的限时权益状态.
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
            await limited_event(session, result, "LIMITED_GRANTED", now)
            return result

    async def activate_for_scene(
        self, user_id: str, scene_id: str, now: datetime
    ) -> LimitedEntitlement | None:
        # 功能: 首次访问活动场景时激活对应限时权益.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     scene_id: 学习场景公开标识.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 匹配的限时权益;无可匹配权益时为 None.
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
        # 功能: 锁定限时权益并执行状态变更,保存幂等和审计信息.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     entitlement_id: 待查询或变更的权益标识.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     operation: 待执行的业务命令,例如授予,暂停,恢复或撤销.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        #     calculator: 根据当前权益,操作命令和时间计算新权益的回调.
        #     reason: 业务状态变更的原因说明,供校验和审计记录.
        # 返回: 计算或持久化后的限时权益状态.
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
        # 功能: 检查同一主体和幂等键的历史执行结果及请求一致性.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session: 当前 SQLAlchemy 异步数据库会话,在调用方事务内执行读写.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     request_hash: 规范化业务请求的摘要,用于检测同一幂等键被不同请求复用.
        # 返回: 匹配的限时权益;无可匹配权益时为 None.
        row = (
            await session.execute(
                text(
                    "SELECT request_hash, after_summary "
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
        values = json.loads(row.after_summary)
        for key in ("granted_at", "start_deadline", "activated_at", "expires_at"):
            values[key] = None if values[key] is None else datetime.fromisoformat(values[key])
        values["scene_ids"] = tuple(values["scene_ids"])
        return LimitedEntitlement(**values)

    async def _select_by_internal_id(
        self,
        session: AsyncSession,
        entitlement_id: int,
        *,
        for_update: bool = False,
    ) -> LimitedEntitlement | None:
        # 功能: 按数据库内部主键读取限时权益记录.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session: 当前 SQLAlchemy 异步数据库会话,在调用方事务内执行读写.
        #     entitlement_id: 权益数据库数值主键.
        #     for_update: 是否对查询结果加行锁,以保护同事务中的更新.
        # 返回: 匹配的限时权益;无可匹配权益时为 None.
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
        # 功能: 按新领域状态更新限时权益数据库记录.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session: 当前 SQLAlchemy 异步数据库会话,在调用方事务内执行读写.
        #     entitlement: 当前权益领域对象,提供状态,期限和业务关联信息.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 无返回值;正常完成表示本次操作成功.
        previous_status = await session.scalar(
            text("SELECT status FROM limited_entitlement WHERE public_id=:id"),
            {"id": entitlement.id},
        )
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

        if previous_status != entitlement.status:
            kind = {"START_EXPIRED": "LIMITED_START_EXPIRED", "ENDED": "LIMITED_EXPIRED"}.get(
                entitlement.status, "LIMITED_STATUS_CHANGED"
            )
            if previous_status == "PENDING" and entitlement.status == "ACTIVE":
                kind = "LIMITED_STARTED"
            await limited_event(session, entitlement, kind, now)

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
        # 功能: 保存限时权益命令记录及操作前后审计摘要.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session: 当前 SQLAlchemy 异步数据库会话,在调用方事务内执行读写.
        #     result: 命令执行完成的业务结果,写入审计或幂等响应.
        #     internal_id: 数据库中业务对象的数值主键.
        #     operation: 待执行的业务命令,例如授予,暂停,恢复或撤销.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     request_hash: 规范化业务请求的摘要,用于检测同一幂等键被不同请求复用.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        #     reason: 业务状态变更的原因说明,供校验和审计记录.
        #     before: 操作前的业务快照;首次创建时可为 None.
        # 返回: 无返回值;正常完成表示本次操作成功.
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
        # 功能: 初始化限时权益对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session_factory: 创建 SQLAlchemy 异步会话的工厂,每次操作独立管理事务.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self._session_factory = session_factory

    async def active_grants(
        self, user_id: str, scene_id: str, now: datetime
    ) -> tuple[AccessGrant, ...]:
        # 功能: 查询当前用户对指定场景有效的权益授权.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     scene_id: 学习场景公开标识.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 符合用户,场景和时效条件的访问授权集合.
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
    # 功能: 对限时权益命令的主体,目标和模式生成幂等摘要.
    # 参数:
    #     operation: 待执行的业务命令,例如授予,暂停,恢复或撤销.
    #     first: 命令的首个业务标识,开通时为用户标识,其他操作时为权益标识.
    #     second: 命令摘要的第二个内容,开通时为活动版本标识,其他操作时为原因说明.
    #     mode: 限时权益启动窗口补救模式;None 表示当前命令无补救模式.
    # 返回: 限时权益操作参数的 SHA-256 摘要.
    value = json.dumps([operation, first, second, mode], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc(value: datetime | None) -> datetime | None:
    # 功能: 将数据库无时区时间补为 UTC 并保留空值.
    # 参数:
    #     value: 待规范化时区或转换业务日期的时间;None 保留为空.
    # 返回: 规范化日期时间;输入为空或允许空值时为 None.
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _from_row(row: Any, scene_ids: tuple[str, ...]) -> LimitedEntitlement:
    # 功能: 将数据库记录转换为限时权益领域对象.
    # 参数:
    #     row: 查询得到的限时权益数据库记录.
    #     scene_ids: 权益或活动版本绑定的固定场景公开标识集合.
    # 返回: 计算或持久化后的限时权益状态.
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
    # 功能: 将限时权益关键字段编码为审计 JSON 摘要.
    # 参数:
    #     entitlement: 当前权益领域对象,提供状态,期限和业务关联信息.
    # 返回: 日期字段转换为 ISO 字符串后的限时权益 JSON 摘要.
    values = asdict(entitlement)
    for key in ("granted_at", "start_deadline", "activated_at", "expires_at"):
        value = values[key]
        values[key] = None if value is None else value.isoformat()
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))
