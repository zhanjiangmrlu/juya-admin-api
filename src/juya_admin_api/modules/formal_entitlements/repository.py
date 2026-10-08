import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta, timezone
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
    async def get(self, user_id: str, package_id: str) -> FormalEntitlement | None:
        # 功能: 读取指定正式权益记录,不存在时返回 None.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     package_id: 正式权益关联的课程套餐公开标识.
        # 返回: 匹配的正式权益;不存在时为 None.
        ...

    async def apply(
        self,
        command: FormalEntitlementCommand,
        actor: str,
        idempotency_key: str,
        now: datetime,
        calculator: OperationCalculator,
    ) -> FormalEntitlement:
        # 功能: 执行正式权益命令并保存权益,幂等结果及审计.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     command: 正式权益操作命令,包含用户,套餐,操作及期限.
        #     actor: 执行权益命令的管理员标识.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        #     calculator: 根据当前权益,操作命令和时间计算新权益的回调.
        # 返回: 计算或持久化后的正式权益状态.
        ...


class InMemoryFormalEntitlementRepository:
    def __init__(self) -> None:
        # 功能: 初始化正式权益对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self.entitlements: dict[tuple[str, str], FormalEntitlement] = {}
        self.operations: dict[tuple[str, str], tuple[str, FormalEntitlement]] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def get(self, user_id: str, package_id: str) -> FormalEntitlement | None:
        # 功能: 读取指定正式权益记录,不存在时返回 None.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     package_id: 正式权益关联的课程套餐公开标识.
        # 返回: 匹配的正式权益;不存在时为 None.
        return self.entitlements.get((user_id, package_id))

    async def apply(
        self,
        command: FormalEntitlementCommand,
        actor: str,
        idempotency_key: str,
        now: datetime,
        calculator: OperationCalculator,
    ) -> FormalEntitlement:
        # 功能: 执行正式权益命令并保存权益,幂等结果及审计.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     command: 正式权益操作命令,包含用户,套餐,操作及期限.
        #     actor: 执行权益命令的管理员标识.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        #     calculator: 根据当前权益,操作命令和时间计算新权益的回调.
        # 返回: 计算或持久化后的正式权益状态.
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
    # 功能: 将数据库无时区时间补为 UTC 并保留空值.
    # 参数:
    #     value: 待规范化时区或转换业务日期的时间;None 保留为空.
    # 返回: 规范化日期时间;输入为空或允许空值时为 None.
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


class SQLAlchemyFormalEntitlementRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        # 功能: 初始化正式权益对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session_factory: 创建 SQLAlchemy 异步会话的工厂,每次操作独立管理事务.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self._session_factory = session_factory

    async def get(self, user_id: str, package_id: str) -> FormalEntitlement | None:
        # 功能: 读取指定正式权益记录,不存在时返回 None.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     package_id: 正式权益关联的课程套餐公开标识.
        # 返回: 匹配的正式权益;不存在时为 None.
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
        # 功能: 执行正式权益命令并保存权益,幂等结果及审计.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     command: 正式权益操作命令,包含用户,套餐,操作及期限.
        #     actor: 执行权益命令的管理员标识.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        #     calculator: 根据当前权益,操作命令和时间计算新权益的回调.
        # 返回: 计算或持久化后的正式权益状态.
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
        # 功能: 按用户与套餐读取正式权益,可选加行锁.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session: 当前 SQLAlchemy 异步数据库会话,在调用方事务内执行读写.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     package_id: 正式权益关联的课程套餐公开标识.
        #     for_update: 是否对查询结果加行锁,以保护同事务中的更新.
        # 返回: 匹配的正式权益;不存在时为 None.
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
        # 功能: 按数据库内部主键读取正式权益记录.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session: 当前 SQLAlchemy 异步数据库会话,在调用方事务内执行读写.
        #     entitlement_id: 权益数据库数值主键.
        # 返回: 匹配的正式权益;不存在时为 None.
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
        # 功能: 初始化正式权益对象并保存依赖及运行状态.
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
        # 功能: 初始化正式权益对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session_factory: 创建 SQLAlchemy 异步会话的工厂,每次操作独立管理事务.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self._session_factory = session_factory

    async def list_entitlements(
        self, filters: dict[str, str], page: int, page_size: int
    ) -> dict[str, Any]:
        # 功能: 按用户,类型,状态和到期筛选分页查询权益.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     filters: 业务列表的筛选条件映射,空映射表示不限.
        #     page: 分页页码,从 1 开始,默认第 1 页.
        #     page_size: 每页返回条数,接口范围为 1 至 100,默认 20.
        # 返回: items 正式及限时权益列表和 page,page_size,total 分页字段.
        allowed = {
            "user_id",
            "type",
            "status",
            "package_id",
            "campaign_id",
            "campaign_version_id",
            "expiry",
            "date_from",
            "date_to",
        }
        if set(filters) - allowed or filters.get("type") not in {None, "FORMAL", "LIMITED"}:
            raise AppError("ENTITLEMENT_FILTER_INVALID", "权益筛选条件不正确", 422)
        if filters.get("status") not in {
            None,
            "ACTIVE",
            "PAUSED",
            "REVOKED",
            "PENDING",
            "ENDED",
            "START_EXPIRED",
            "EXPIRED",
        }:
            raise AppError("ENTITLEMENT_FILTER_INVALID", "权益状态筛选不正确", 422)
        if filters.get("expiry") not in {None, "EXPIRING", "ENDING", "START_EXPIRING"}:
            raise AppError("ENTITLEMENT_FILTER_INVALID", "权益预警筛选不正确", 422)
        if page < 1 or not 1 <= page_size <= 100:
            raise AppError("PAGINATION_INVALID", "分页参数不正确", 422)
        now = datetime.now(UTC)
        query = (
            "SELECT fe.public_id AS id,'FORMAL' AS type,u.public_id AS "
            "user_id,u.juya_number,profile.nickname, "
            "COALESCE(contact.contact_status,'NOT_PROVIDED') AS contact_status, "
            "CASE WHEN fe.status IN ('ACTIVE','PAUSED') AND fe.expires_at<=:now THEN "
            "'EXPIRED' ELSE fe.status END AS status, "
            "fe.granted_at,fe.granted_at AS "
            "effective_at,fe.expires_at,p.public_id AS package_id,p.name AS "
            "content_name, "
            "NULL AS campaign_id,NULL AS campaign_version_id,NULL AS "
            "campaign_version_no,fe.term,NULL AS start_deadline "
            "FROM formal_entitlement fe JOIN user_account u ON u.id=fe.user_id "
            "JOIN content_package p ON p.id=fe.package_id LEFT JOIN "
            "user_profile profile ON profile.user_id=u.id "
            "LEFT JOIN user_contact contact ON contact.user_id=u.id UNION ALL "
            "SELECT le.public_id,'LIMITED',u.public_id,u.juya_number,profile.nickname, "
            "COALESCE(contact.contact_status,'NOT_PROVIDED'), "
            "CASE WHEN le.status='PENDING' AND le.start_deadline<=:now THEN 'START_EXPIRED' "
            "WHEN le.status='ACTIVE' AND le.expires_at<=:now THEN 'ENDED' ELSE le.status END, "
            "le.granted_at,le.activated_at,le.expires_at,NULL,c.name,c.pu"
            "blic_id,cv.public_id,cv.version_no, "
            "CONCAT(cv.duration_days,'_DAYS'),le.start_deadline "
            "FROM limited_entitlement le JOIN user_account u ON u.id=le.user_id "
            "JOIN limited_campaign_version cv ON cv.id=le.campaign_version_id "
            "JOIN limited_campaign c ON c.id=cv.campaign_id LEFT JOIN "
            "user_profile profile ON profile.user_id=u.id "
            "LEFT JOIN user_contact contact ON contact.user_id=u.id"
        )
        conditions: list[str] = []
        params: dict[str, Any] = {"limit": page_size, "offset": (page - 1) * page_size, "now": now}
        for key in {
            "user_id",
            "type",
            "status",
            "package_id",
            "campaign_id",
            "campaign_version_id",
        }:
            if key in filters:
                conditions.append(f"v.{key}=:{key}")
                params[key] = filters[key]
        for key in ("date_from", "date_to"):
            if key in filters:
                try:
                    day = datetime.strptime(filters[key], "%Y-%m-%d").replace(
                        tzinfo=timezone(timedelta(hours=8))
                    )
                except ValueError as error:
                    raise AppError("ENTITLEMENT_FILTER_INVALID", "日期格式不正确", 422) from error
                params[key] = (day + timedelta(days=1) if key == "date_to" else day).astimezone(UTC)
                conditions.append(f"v.granted_at {'<' if key == 'date_to' else '>='} :{key}")
        if (
            "date_from" in params
            and "date_to" in params
            and params["date_from"] >= params["date_to"]
        ):
            raise AppError("ENTITLEMENT_FILTER_INVALID", "结束日期不得早于开始日期", 422)
        async with self._session_factory() as session:
            if expiry := filters.get("expiry"):
                warning = await session.scalar(
                    text(
                        "SELECT JSON_UNQUOTE(JSON_EXTRACT(value,'$.value')) FROM "
                        "system_config WHERE config_key='entitlement_expiry_warning_days'"
                    )
                )
                params["warning"] = now + timedelta(days=int(warning or 30))
                params["soon"] = now + timedelta(hours=24)
                conditions.append(
                    {
                        "EXPIRING": (
                            "v.type='FORMAL' AND v.status='ACTIVE' AND v.expires_at>:now AND "
                            "v.expires_at<=:warning"
                        ),
                        "ENDING": (
                            "v.type='LIMITED' AND v.status='ACTIVE' AND v.expires_at>:now AND "
                            "v.expires_at<=:soon"
                        ),
                        "START_EXPIRING": (
                            "v.type='LIMITED' AND v.status='PENDING' AND "
                            "v.start_deadline>:now AND v.start_deadline<=:soon"
                        ),
                    }[expiry]
                )
            where = " WHERE " + " AND ".join(conditions) if conditions else ""
            total = await session.scalar(text(f"SELECT COUNT(*) FROM ({query}) v{where}"), params)
            rows = (
                (
                    await session.execute(
                        text(
                            f"SELECT * FROM ({query}) v{where} "
                            "ORDER BY v.granted_at DESC,v.id DESC LIMIT :limit OFFSET :offset"
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
        return {
            "items": [
                {
                    key: _utc(value) if isinstance(value, datetime) else value
                    for key, value in row.items()
                }
                for row in rows
            ],
            "page": page,
            "page_size": page_size,
            "total": total or 0,
        }

    async def get_formal(self, entitlement_id: str) -> dict[str, Any] | None:
        # 功能: 查询正式权益及关联用户,套餐详情.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     entitlement_id: 待查询或变更的权益标识.
        # 返回: 正式权益及套餐名称,包含期限,到期时间,版本号和可用操作.无匹配权益时为 None.
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
        # 功能: 查询限时权益,关联活动,固定场景和当前允许的操作.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     entitlement_id: 待查询或变更的权益标识.
        # 返回: 限时权益及关联活动信息,固定场景标识和可用操作.无匹配权益时为 None.
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
        # 匿名函数: 预览延长未激活限时权益启动截止时间的补救结果.
        # 参数: 无.
        # 返回: 校验补救次数及状态后的限时权益.
        # 匿名函数: 预览为启动窗口已失效权益重建启动窗口的补救结果.
        # 参数: 无.
        # 返回: 校验补救次数及状态后的限时权益.
        # 匿名函数: 计算当前限时权益暂停后的状态.
        # 参数: 无.
        # 返回: 暂停后且版本递增的限时权益.
        # 匿名函数: 计算当前限时权益恢复后的状态.
        # 参数: 无.
        # 返回: 恢复激活且版本递增的限时权益.
        # 匿名函数: 计算当前限时权益撤销后的状态.
        # 参数: 无.
        # 返回: 撤销后且版本递增的限时权益.
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
        # 功能: 分页查询可用于正式权益授予的课程套餐.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     page: 分页页码,从 1 开始,默认第 1 页.
        #     page_size: 每页返回条数,接口范围为 1 至 100,默认 20.
        # 返回: items 启用套餐列表和 page,page_size,total;每项含 id,name,status,sort_order.
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
    # 功能: 将数据库记录转换为正式权益领域对象.
    # 参数:
    #     row: 查询得到的正式权益数据库记录.
    # 返回: 计算或持久化后的正式权益状态.
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
    # 功能: 提取正式权益关键字段作为审计摘要.
    # 参数:
    #     entitlement: 当前权益领域对象,提供状态,期限和业务关联信息.
    # 返回: 权益标识,用户及套餐标识,状态,期限,开通和到期时间,版本号.期限转为枚举值,
    #     开通和到期时间转为 ISO 字符串.
    values = asdict(entitlement)
    values["term"] = entitlement.term.value
    for key in ("granted_at", "expires_at"):
        value = values[key]
        values[key] = None if value is None else value.isoformat()
    return values
