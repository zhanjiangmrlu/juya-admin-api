import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.infrastructure.db.admin_identity import resolve_admin_names
from juya_admin_api.shared.ids import new_ulid


@dataclass(frozen=True, slots=True)
class AuditEvent:
    actor_public_id: str | None
    action: str
    object_type: str
    object_public_id: str
    before_summary: dict[str, object]
    after_summary: dict[str, object]
    reason: str | None
    request_id: str
    occurred_at: datetime
    actor_name: str | None = None


class AuditRepository(Protocol):
    async def append(self, event: AuditEvent) -> None:
        # 功能: 追加业务审计事件.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     event: 审计事件, 包含操作主体,对象,前后摘要及发生时间.
        # 返回: 无返回值;正常完成表示本次操作成功.
        ...

    async def list_recent(self, limit: int) -> list[AuditEvent]:
        # 功能: 按时间倒序查询最近的业务审计事件.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     limit: 最近记录的最大返回条数.
        # 返回: 最近的审计事件列表.
        ...


class AuditService:
    def __init__(self, repository: AuditRepository) -> None:
        # 功能: 初始化审计事件对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     repository: 提供审计事件持久化和查询能力的仓储.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self._repository = repository

    @staticmethod
    def summarize(
        values: dict[str, object], *, allowed_fields: frozenset[str]
    ) -> dict[str, object]:
        # 功能: 仅保留审计白名单中的已存在字段,按字段名排序并原样保留值.
        # 参数:
        #     values: 待生成安全审计摘要的业务字段映射.
        #     allowed_fields: 审计摘要允许保留的字段白名单.
        # 返回: 仅包含白名单与输入交集字段的字典,键按名称排序,字段值原样保留.
        return {key: values[key] for key in sorted(allowed_fields) if key in values}

    async def record(self, event: AuditEvent) -> None:
        # 功能: 记录经过业务构造的审计事件.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     event: 审计事件, 包含操作主体,对象,前后摘要及发生时间.
        # 返回: 无返回值;正常完成表示本次操作成功.
        await self._repository.append(event)

    async def list_recent(self, limit: int = 100) -> list[AuditEvent]:
        # 功能: 按时间倒序查询最近的业务审计事件.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     limit: 最近记录的最大返回条数.
        # 返回: 最近的审计事件列表.
        return await self._repository.list_recent(min(max(limit, 1), 200))


class SQLAlchemyAuditRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        # 功能: 初始化审计事件对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session_factory: 创建 SQLAlchemy 异步会话的工厂,每次操作独立管理事务.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self._session_factory = session_factory

    async def append(self, event: AuditEvent) -> None:
        # 功能: 追加业务审计事件.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     event: 审计事件, 包含操作主体,对象,前后摘要及发生时间.
        # 返回: 无返回值;正常完成表示本次操作成功.
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO audit_event "
                    "(public_id, actor_public_id, action, object_type, object_public_id, "
                    "before_summary, after_summary, reason, request_id, created_at) VALUES "
                    "(:public_id, :actor, :action, :object_type, :object_id, :before_summary, "
                    ":after_summary, :reason, :request_id, :created_at)"
                ),
                {
                    "public_id": new_ulid(event.occurred_at),
                    "actor": event.actor_public_id,
                    "action": event.action,
                    "object_type": event.object_type,
                    "object_id": event.object_public_id,
                    "before_summary": json.dumps(
                        event.before_summary, ensure_ascii=False, separators=(",", ":")
                    ),
                    "after_summary": json.dumps(
                        event.after_summary, ensure_ascii=False, separators=(",", ":")
                    ),
                    "reason": event.reason,
                    "request_id": event.request_id,
                    "created_at": event.occurred_at,
                },
            )

    async def list_recent(self, limit: int) -> list[AuditEvent]:
        # 功能: 按时间倒序查询最近的业务审计事件.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     limit: 最近记录的最大返回条数.
        # 返回: 最近的审计事件列表.
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT actor_public_id, action, object_type, object_public_id, "
                        "before_summary, after_summary, reason, request_id, created_at "
                        "FROM audit_event ORDER BY created_at DESC LIMIT :limit"
                    ),
                    {"limit": limit},
                )
            ).all()
            names = await resolve_admin_names(session, [row.actor_public_id for row in rows])
        events: list[AuditEvent] = []
        for row in rows:
            occurred_at = row.created_at
            if occurred_at.tzinfo is None:
                occurred_at = occurred_at.replace(tzinfo=UTC)
            events.append(
                AuditEvent(
                    actor_public_id=row.actor_public_id,
                    actor_name=names.get(row.actor_public_id),
                    action=row.action,
                    object_type=row.object_type,
                    object_public_id=row.object_public_id,
                    before_summary=_decode_summary(row.before_summary),
                    after_summary=_decode_summary(row.after_summary),
                    reason=row.reason,
                    request_id=row.request_id,
                    occurred_at=occurred_at,
                )
            )
        return events


def _decode_summary(value: object) -> dict[str, object]:
    # 功能: 解析审计摘要中的 JSON 对象.
    # 参数:
    #     value: 数据库或业务存储中的 JSON 文本或已解码对象.
    # 返回: 解析后的审计摘要字典.
    """Normalize JSON objects returned by typed and raw SQL drivers."""
    decoded: object = json.loads(value) if isinstance(value, str) else value
    if decoded is None:
        return {}
    if not isinstance(decoded, Mapping):
        raise TypeError("audit summary must be a JSON object")
    return {str(key): item for key, item in decoded.items()}
