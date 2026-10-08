"""Transactional campaign administration backed by the existing campaign tables."""

import json
from datetime import UTC, datetime
from typing import Any, cast

from fastapi.encoders import jsonable_encoder
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.campaigns.domain import CampaignDuration, CampaignVersion
from juya_admin_api.modules.campaigns.service import CampaignService
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.idempotency import _json_body
from juya_admin_api.shared.ids import new_ulid

CAMPAIGN_TARGET_STATUSES = {
    "open": "OPEN",
    "pause": "PAUSED",
    "resume": "OPEN",
    "end": "ENDED",
    "archive": "ARCHIVED",
}
CAMPAIGN_ALLOWED_STATUSES = {
    "open": {"DRAFT"},
    "pause": {"OPEN"},
    "resume": {"PAUSED"},
    "end": {"OPEN", "PAUSED"},
    "archive": {"ENDED", "CLOSED"},
    "copy": {"DRAFT", "ENDED", "CLOSED"},
}


def campaign_available_operations(status: str, has_version: bool) -> list[str]:
    # 功能: 根据活动状态和版本存在情况列出允许的操作.
    # 参数:
    #     status: 活动,反馈或权益的业务状态筛选条件.
    #     has_version: 活动是否已经创建版本,影响可执行操作集合.
    # 返回: 允许执行的活动操作名称列表.
    """Expose operations accepted by the authoritative repository state checks."""
    if not has_version:
        return []
    return [
        operation
        for operation in ("open", "pause", "resume", "end", "archive", "copy", "capacity")
        if operation == "capacity" or status in CAMPAIGN_ALLOWED_STATUSES[operation]
    ]


class SQLAlchemyCampaignRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        # 功能: 初始化限时活动对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session_factory: 创建 SQLAlchemy 异步会话的工厂,每次操作独立管理事务.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self._session_factory = session_factory

    async def list(self, filters: dict[str, str], page: int, page_size: int) -> dict[str, Any]:
        # 功能: 按条件读取限时活动列表及分页信息.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     filters: 业务列表的筛选条件映射,空映射表示不限.
        #     page: 分页页码,从 1 开始,默认第 1 页.
        #     page_size: 每页返回条数,接口范围为 1 至 100,默认 20.
        # 返回: items 活动列表及 page,page_size,total 分页字段;各项包含当前版本及可用操作.
        if set(filters) - {"status"} or (
            "status" in filters
            and filters["status"] not in {"DRAFT", "OPEN", "PAUSED", "ENDED", "ARCHIVED", "CLOSED"}
        ):
            raise AppError("CAMPAIGN_FILTER_INVALID", "活动筛选条件不正确", 422)
        if page < 1 or not 1 <= page_size <= 100:
            raise AppError("PAGINATION_INVALID", "分页参数不正确", 422)
        where = " WHERE c.status = :status" if "status" in filters else ""
        params: dict[str, Any] = {**filters, "limit": page_size, "offset": (page - 1) * page_size}
        async with self._session_factory() as session:
            total = await session.scalar(
                text("SELECT COUNT(*) FROM limited_campaign c" + where), params
            )
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT c.public_id AS id, c.name, c.status, c.version, "
                            "cv.public_id AS current_version_id, cv.capacity, "
                            "cv.granted_user_count, "
                            "c.created_at, c.updated_at FROM limited_campaign c "
                            "LEFT JOIN limited_campaign_version cv ON cv.id = c.current_version_id"
                            + where
                            + " ORDER BY c.created_at DESC, c.id DESC LIMIT :limit OFFSET :offset"
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
            item["created_at"] = _utc(item["created_at"])
            item["updated_at"] = _utc(item["updated_at"])
            item["available_operations"] = campaign_available_operations(
                item["status"], item["current_version_id"] is not None
            )
            items.append(item)
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total or 0,
        }

    async def get(self, campaign_id: str) -> dict[str, Any] | None:
        # 功能: 读取指定限时活动记录,不存在时返回 None.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     campaign_id: 限时活动公开标识;创建活动时可为 None.
        # 返回: 活动标识,名称,状态,版本和时间,以及含期限,容量,场景的 current_version 和可用操作.
        #     不存在时为 None.
        async with self._session_factory() as session:
            return await self._get(session, campaign_id)

    async def _get(self, session: AsyncSession, campaign_id: str) -> dict[str, Any] | None:
        # 功能: 在给定会话内读取活动及版本,场景信息.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session: 当前 SQLAlchemy 异步数据库会话,在调用方事务内执行读写.
        #     campaign_id: 限时活动公开标识;创建活动时可为 None.
        # 返回: 活动标识,名称,状态,版本和时间,以及含期限,容量,场景的 current_version 和可用操作.
        #     不存在时为 None.
        row = (
            (
                await session.execute(
                    text(
                        "SELECT id, public_id, name, status, version, current_version_id, "
                        "created_at, updated_at FROM limited_campaign WHERE public_id = :id"
                    ),
                    {"id": campaign_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        result = {
            key: row[key] for key in ("name", "status", "version", "created_at", "updated_at")
        }
        result["created_at"] = _utc(result["created_at"])
        result["updated_at"] = _utc(result["updated_at"])
        result["id"] = row["public_id"]
        version = (
            (
                await session.execute(
                    text(
                        "SELECT public_id AS id, version_no, status, duration_days, "
                        "activation_window_days, capacity, granted_user_count, grant_starts_at, "
                        "grant_ends_at, locked_at, version "
                        "FROM limited_campaign_version WHERE id = :id"
                    ),
                    {"id": row["current_version_id"]},
                )
            )
            .mappings()
            .first()
            if row["current_version_id"]
            else None
        )
        if version is None:
            result["current_version"] = None
        else:
            scenes: list[str] = list(
                (
                    await session.execute(
                        text(
                            "SELECT s.public_id FROM limited_campaign_scene cs "
                            "JOIN scene s ON s.id = cs.scene_id "
                            "WHERE cs.campaign_version_id = :id ORDER BY cs.position"
                        ),
                        {"id": row["current_version_id"]},
                    )
                )
                .scalars()
                .all()
            )
            current_version = {**dict(version), "scene_ids": list(scenes)}
            for field in ("grant_starts_at", "grant_ends_at", "locked_at"):
                current_version[field] = _utc(current_version[field])
            result["current_version"] = current_version
        result["available_operations"] = campaign_available_operations(
            result["status"], version is not None
        )
        return result

    async def save(
        self,
        campaign_id: str | None,
        *,
        expected_version: int | None = None,
        name: str,
        now: datetime,
        duration_days: int | None = None,
        activation_window_days: int | None = None,
        capacity: int | None = None,
        scene_ids: tuple[str, ...] | None = None,
        actor_id: str = "system",
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> dict[str, Any]:
        # 功能: 创建或更新活动及版本字段,保证版本一致性和请求幂等.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     campaign_id: 限时活动公开标识;创建活动时可为 None.
        #     expected_version: 调用方读取到的版本号,写入时用于检测并发更新.
        #     name: 活动展示名称.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        #     duration_days: 限时权益激活后的有效天数,只接受 3 或 5 天.
        #     activation_window_days: 开通后允许首次启动的窗口长度,单位为天.
        #     capacity: 活动版本允许累计开通的用户数量,不能低于已开通人数.
        #     scene_ids: 权益或活动版本绑定的固定场景公开标识集合.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     request_hash: 规范化业务请求的摘要,用于检测同一幂等键被不同请求复用.
        # 返回: 活动标识,名称,状态,版本和时间,以及含期限,容量,场景的 current_version 和可用操作.
        #     重试返回已保存幂等响应.
        if not name.strip() or len(name) > 200:
            raise AppError("CAMPAIGN_NAME_INVALID", "活动名称不正确", 422)
        async with self._session_factory() as session, session.begin():
            replay = await self._claim_idempotency(
                session, "campaign.save", actor_id, idempotency_key, request_hash
            )
            if replay is not None:
                return replay
            if campaign_id is None:
                if duration_days is None or activation_window_days is None or capacity is None:
                    raise AppError("CAMPAIGN_FIELDS_REQUIRED", "活动版本字段不能为空", 422)
                campaign_id = new_ulid(now)
                version_id = new_ulid(now)
                version = CampaignVersion(
                    version_id,
                    campaign_id,
                    "DRAFT",
                    cast(CampaignDuration, duration_days),
                    activation_window_days,
                    capacity,
                    0,
                    scene_ids or (),
                )
                version.validate()
                await session.execute(
                    text(
                        "INSERT INTO limited_campaign "
                        "(public_id, name, status, version, updated_at) "
                        "VALUES (:id, :name, 'DRAFT', 1, :now)"
                    ),
                    {"id": campaign_id, "name": name.strip(), "now": now},
                )
                internal_id = await session.scalar(
                    text("SELECT id FROM limited_campaign WHERE public_id = :id"),
                    {"id": campaign_id},
                )
                await session.execute(
                    text(
                        "INSERT INTO limited_campaign_version (public_id, campaign_id, version_no, "
                        "status, duration_days, activation_window_days, "
                        "capacity, version, updated_at) "
                        "VALUES (:id, :campaign, 1, 'DRAFT', :duration, "
                        ":window, :capacity, 1, :now)"
                    ),
                    {
                        "id": version_id,
                        "campaign": internal_id,
                        "duration": duration_days,
                        "window": activation_window_days,
                        "capacity": capacity,
                        "now": now,
                    },
                )
                version_internal_id = await session.scalar(
                    text("SELECT id FROM limited_campaign_version WHERE public_id = :id"),
                    {"id": version_id},
                )
                await self._replace_scenes(session, version_internal_id, scene_ids or ())
                await session.execute(
                    text(
                        "UPDATE limited_campaign SET current_version_id = :version WHERE id = :id"
                    ),
                    {"version": version_internal_id, "id": internal_id},
                )
                before = None
            else:
                row = await self._lock_campaign(session, campaign_id, expected_version)
                if row.status != "DRAFT":
                    raise AppError("CAMPAIGN_EDIT_STATE_CONFLICT", "仅草稿活动可以编辑", 409)
                before = await self._get(session, campaign_id)
                version_row = (
                    await session.execute(
                        text("SELECT * FROM limited_campaign_version WHERE id = :id FOR UPDATE"),
                        {"id": row.current_version_id},
                    )
                ).first()
                assert version_row is not None
                current = await self._get(session, campaign_id)
                assert current is not None and current["current_version"] is not None
                existing = current["current_version"]
                version = CampaignVersion(
                    existing["id"],
                    campaign_id,
                    existing["status"],
                    existing["duration_days"],
                    existing["activation_window_days"],
                    existing["capacity"],
                    existing["granted_user_count"],
                    tuple(existing["scene_ids"]),
                    existing["version"],
                    _utc(existing["locked_at"]),
                )
                CampaignService.revise_version(
                    version,
                    duration_days=cast(CampaignDuration, duration_days)
                    if duration_days is not None
                    else version.duration_days,
                    activation_window_days=(
                        activation_window_days
                        if activation_window_days is not None
                        else version.activation_window_days
                    ),
                    capacity=capacity if capacity is not None else version.capacity,
                    scene_ids=scene_ids if scene_ids is not None else version.scene_ids,
                )
                await session.execute(
                    text(
                        "UPDATE limited_campaign SET name = :name, version = version + 1, "
                        "updated_at = :now WHERE id = :id"
                    ),
                    {"name": name.strip(), "now": now, "id": row.id},
                )
                await session.execute(
                    text(
                        "UPDATE limited_campaign_version SET duration_days = :duration, "
                        "activation_window_days = :window, capacity = :capacity, "
                        "version = version + 1, updated_at = :now WHERE id = :id"
                    ),
                    {
                        "duration": version.duration_days,
                        "window": version.activation_window_days,
                        "capacity": version.capacity,
                        "now": now,
                        "id": version_row.id,
                    },
                )
                if scene_ids is not None:
                    await self._replace_scenes(session, version_row.id, scene_ids)
                internal_id = row.id
            result = await self._get(session, campaign_id)
            assert result is not None
            await self._audit(session, internal_id, "SAVE", before, result, actor_id, now)
            await self._complete_idempotency(
                session,
                "campaign.save",
                actor_id,
                idempotency_key,
                result,
                status_code=201 if before is None else 200,
            )
            return result

    async def command(
        self,
        campaign_id: str,
        operation: str,
        *,
        expected_version: int,
        now: datetime,
        capacity: int | None = None,
        actor_id: str = "system",
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> dict[str, Any]:
        # 功能: 执行活动版本命令,检查状态及版本并记录幂等和审计.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     campaign_id: 限时活动公开标识;创建活动时可为 None.
        #     operation: 待执行的业务命令,例如授予,暂停,恢复或撤销.
        #     expected_version: 调用方读取到的版本号,写入时用于检测并发更新.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        #     capacity: 活动版本允许累计开通的用户数量,不能低于已开通人数.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     request_hash: 规范化业务请求的摘要,用于检测同一幂等键被不同请求复用.
        # 返回: 活动标识,名称,状态,版本和时间,以及含期限,容量,场景的 current_version 和可用操作.
        #     重试返回已保存幂等响应.
        if operation not in {*CAMPAIGN_TARGET_STATUSES, "capacity", "copy"}:
            raise AppError("CAMPAIGN_OPERATION_INVALID", "活动操作不支持", 422)
        async with self._session_factory() as session, session.begin():
            replay = await self._claim_idempotency(
                session, "campaign.command", actor_id, idempotency_key, request_hash
            )
            if replay is not None:
                return replay
            row = await self._lock_campaign(session, campaign_id, expected_version)
            before = await self._get(session, campaign_id)
            version = (
                await session.execute(
                    text(
                        "SELECT id, capacity, granted_user_count FROM limited_campaign_version "
                        "WHERE id = :id FOR UPDATE"
                    ),
                    {"id": row.current_version_id},
                )
            ).first()
            if version is None:
                raise AppError("CAMPAIGN_VERSION_NOT_FOUND", "活动版本不存在", 404)
            if operation == "copy":
                if row.status not in CAMPAIGN_ALLOWED_STATUSES["copy"]:
                    raise AppError("CAMPAIGN_STATE_CONFLICT", "当前活动状态不可复制版本", 409)
                new_id = new_ulid(now)
                await session.execute(
                    text(
                        "INSERT INTO limited_campaign_version "
                        "(public_id, campaign_id, version_no, status, duration_days, "
                        "activation_window_days, capacity, granted_user_count, grant_starts_at, "
                        "grant_ends_at, locked_at, version, updated_at) "
                        "SELECT :new_id, campaign_id, "
                        "(SELECT MAX(version_no) + 1 FROM (SELECT version_no FROM "
                        "limited_campaign_version WHERE campaign_id = :campaign_id) x), "
                        "'DRAFT', duration_days, activation_window_days, capacity, 0, "
                        "grant_starts_at, grant_ends_at, locked_at, 1, :now "
                        "FROM limited_campaign_version WHERE id = :old_id"
                    ),
                    {"new_id": new_id, "campaign_id": row.id, "old_id": version.id, "now": now},
                )
                new_internal_id = await session.scalar(
                    text("SELECT id FROM limited_campaign_version WHERE public_id = :id"),
                    {"id": new_id},
                )
                await session.execute(
                    text(
                        "INSERT INTO limited_campaign_scene "
                        "(campaign_version_id, scene_id, position, scene_revision_id) "
                        "SELECT :new_id, scene_id, position, scene_revision_id "
                        "FROM limited_campaign_scene WHERE campaign_version_id = :old_id"
                    ),
                    {"new_id": new_internal_id, "old_id": version.id},
                )
                await session.execute(
                    text(
                        "UPDATE limited_campaign SET current_version_id = :version, "
                        "status = 'DRAFT' WHERE id = :id"
                    ),
                    {"version": new_internal_id, "id": row.id},
                )
            elif operation == "capacity":
                if capacity is None or capacity < version.granted_user_count:
                    raise AppError(
                        "CAMPAIGN_CAPACITY_BELOW_GRANTED", "容量不能低于累计开通人数", 409
                    )
                await session.execute(
                    text(
                        "UPDATE limited_campaign_version SET capacity = :capacity, "
                        "version = version + 1, updated_at = :now WHERE id = :id"
                    ),
                    {"capacity": capacity, "now": now, "id": version.id},
                )
            else:
                if row.status not in CAMPAIGN_ALLOWED_STATUSES[operation]:
                    raise AppError("CAMPAIGN_STATE_CONFLICT", "当前活动状态不可执行此操作", 409)
                status = CAMPAIGN_TARGET_STATUSES[operation]
                await session.execute(
                    text("UPDATE limited_campaign SET status = :status WHERE id = :id"),
                    {"status": status, "id": row.id},
                )
                if operation != "archive":
                    await session.execute(
                        text(
                            "UPDATE limited_campaign_version SET status = :status, "
                            "version = version + 1, updated_at = :now WHERE id = :id"
                        ),
                        {"status": status, "now": now, "id": version.id},
                    )
            await session.execute(
                text(
                    "UPDATE limited_campaign SET version = version + 1, "
                    "updated_at = :now WHERE id = :id"
                ),
                {"now": now, "id": row.id},
            )
            result = await self._get(session, campaign_id)
            assert result is not None
            await self._audit(session, row.id, operation.upper(), before, result, actor_id, now)
            await self._complete_idempotency(
                session, "campaign.command", actor_id, idempotency_key, result
            )
            return result

    async def _claim_idempotency(
        self,
        session: AsyncSession,
        scope: str,
        actor_id: str,
        key: str | None,
        request_hash: str | None,
    ) -> dict[str, Any] | None:
        # 功能: 锁定或创建业务幂等记录,检查请求摘要并返回已完成结果.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session: 当前 SQLAlchemy 异步数据库会话,在调用方事务内执行读写.
        #     scope: 幂等记录的业务作用域,隔离不同类型的命令.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     request_hash: 规范化业务请求的摘要,用于检测同一幂等键被不同请求复用.
        # 返回: 已完成请求保存的活动响应;未启用幂等或首次成功占用时为 None.
        if key is None and request_hash is None:
            return None
        if not key or not request_hash:
            raise ValueError("idempotency key and request hash must be supplied together")
        params = {"scope": scope, "actor": actor_id, "key": key, "hash": request_hash}
        inserted = await session.execute(
            text(
                "INSERT IGNORE INTO idempotency_record "
                "(scope, actor_id, idempotency_key, request_hash, status) "
                "VALUES (:scope, :actor, :key, :hash, 'IN_PROGRESS')"
            ),
            params,
        )
        row = (
            await session.execute(
                text(
                    "SELECT request_hash, status, response_body FROM idempotency_record "
                    "WHERE scope = :scope AND actor_id = :actor "
                    "AND idempotency_key = :key FOR UPDATE"
                ),
                params,
            )
        ).one()
        if row.request_hash != request_hash:
            raise AppError("IDEMPOTENCY_KEY_REUSED", "幂等键已用于不同请求", 409)
        if row.status == "COMPLETED":
            return _json_body(row.response_body)
        if getattr(inserted, "rowcount", 0) != 1:
            raise AppError(
                "IDEMPOTENCY_IN_PROGRESS",
                "同键请求结果未完成需人工核查",
                409,
            )
        return None

    async def _complete_idempotency(
        self,
        session: AsyncSession,
        scope: str,
        actor_id: str,
        key: str | None,
        result: dict[str, Any],
        *,
        status_code: int = 200,
    ) -> None:
        # 功能: 保存业务命令完成后的幂等响应.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session: 当前 SQLAlchemy 异步数据库会话,在调用方事务内执行读写.
        #     scope: 幂等记录的业务作用域,隔离不同类型的命令.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     result: 命令执行完成的业务结果,写入审计或幂等响应.
        #     status_code: HTTP 响应状态码.
        # 返回: 无返回值;正常完成表示本次操作成功.
        if key is None:
            return
        await session.execute(
            text(
                "UPDATE idempotency_record SET status = 'COMPLETED', "
                "response_status = :status, response_body = :body, completed_at = "
                "CURRENT_TIMESTAMP(6) WHERE scope = :scope AND actor_id = :actor "
                "AND idempotency_key = :key"
            ),
            {
                "status": status_code,
                "body": json.dumps(jsonable_encoder(result)),
                "scope": scope,
                "actor": actor_id,
                "key": key,
            },
        )

    async def _lock_campaign(
        self, session: AsyncSession, campaign_id: str, expected_version: int | None
    ) -> Any:
        # 功能: 锁定活动记录并检查调用方的预期版本.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session: 当前 SQLAlchemy 异步数据库会话,在调用方事务内执行读写.
        #     campaign_id: 限时活动公开标识;创建活动时可为 None.
        #     expected_version: 调用方读取到的版本号,写入时用于检测并发更新.
        # 返回: 已锁定且版本匹配的活动数据库行,含 id,status,version 和 current_version_id.
        row = (
            await session.execute(
                text(
                    "SELECT id, status, version, current_version_id FROM limited_campaign "
                    "WHERE public_id = :id FOR UPDATE"
                ),
                {"id": campaign_id},
            )
        ).first()
        if row is None:
            raise AppError("CAMPAIGN_NOT_FOUND", "活动不存在", 404)
        if expected_version != row.version:
            raise AppError(
                "CAMPAIGN_VERSION_CONFLICT", "活动版本已变化", 409, {"current_version": row.version}
            )
        return row

    async def _replace_scenes(
        self, session: AsyncSession, version_id: int, scene_ids: tuple[str, ...]
    ) -> None:
        # 功能: 替换指定活动版本绑定的固定场景集合.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session: 当前 SQLAlchemy 异步数据库会话,在调用方事务内执行读写.
        #     version_id: 限时活动版本的数据库数值主键.
        #     scene_ids: 权益或活动版本绑定的固定场景公开标识集合.
        # 返回: 无返回值;正常完成表示本次操作成功.
        if len(set(scene_ids)) != len(scene_ids):
            raise AppError("CAMPAIGN_SCENES_INVALID", "活动场景不可重复", 422)
        await session.execute(
            text("DELETE FROM limited_campaign_scene WHERE campaign_version_id = :id"),
            {"id": version_id},
        )
        for position, scene_id in enumerate(scene_ids, 1):
            scene = (
                await session.execute(
                    text(
                        "SELECT id, published_revision_id FROM scene WHERE public_id = :id "
                        "AND status = 'PUBLISHED'"
                    ),
                    {"id": scene_id},
                )
            ).first()
            if scene is None or scene.published_revision_id is None:
                raise AppError("CAMPAIGN_SCENE_INVALID", "活动场景未发布", 422)
            await session.execute(
                text(
                    "INSERT INTO limited_campaign_scene (campaign_version_id, scene_id, "
                    "position, scene_revision_id) VALUES (:version, :scene, :position, :revision)"
                ),
                {
                    "version": version_id,
                    "scene": scene.id,
                    "position": position,
                    "revision": scene.published_revision_id,
                },
            )

    async def _audit(
        self,
        session: AsyncSession,
        internal_id: int,
        operation: str,
        before: dict[str, Any] | None,
        after: dict[str, Any],
        actor_id: str,
        now: datetime,
    ) -> None:
        # 功能: 记录活动命令执行前后的审计摘要.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session: 当前 SQLAlchemy 异步数据库会话,在调用方事务内执行读写.
        #     internal_id: 数据库中业务对象的数值主键.
        #     operation: 待执行的业务命令,例如授予,暂停,恢复或撤销.
        #     before: 操作前的业务快照;首次创建时可为 None.
        #     after: 操作后的业务快照.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 无返回值;正常完成表示本次操作成功.
        await session.execute(
            text(
                "INSERT INTO limited_campaign_operation (public_id, campaign_id, "
                "operation_type, before_summary, after_summary, operator_id, created_at) "
                "VALUES (:id, :campaign, :operation, :before, :after, :actor, :now)"
            ),
            {
                "id": new_ulid(now),
                "campaign": internal_id,
                "operation": operation,
                "before": None if before is None else json.dumps(_audit_summary(before)),
                "after": json.dumps(_audit_summary(after)),
                "actor": actor_id,
                "now": now,
            },
        )


def _utc(value: datetime | None) -> datetime | None:
    # 功能: 将数据库无时区时间补为 UTC 并保留空值.
    # 参数:
    #     value: 待规范化时区或转换业务日期的时间;None 保留为空.
    # 返回: 规范化日期时间;输入为空或允许空值时为 None.
    return value if value is None or value.tzinfo is not None else value.replace(tzinfo=UTC)


def _audit_summary(campaign: dict[str, Any]) -> dict[str, Any]:
    # 功能: 提取活动快照中允许写入审计的字段.
    # 参数:
    #     campaign: 包含活动及版本信息的业务快照.
    # 返回: 活动标识,状态,版本及当前活动版本标识,容量和累计开通人数的审计摘要.
    version = campaign.get("current_version") or {}
    return {
        "id": campaign["id"],
        "status": campaign["status"],
        "version": campaign["version"],
        "current_version_id": version.get("id"),
        "capacity": version.get("capacity"),
        "granted_user_count": version.get("granted_user_count"),
    }
