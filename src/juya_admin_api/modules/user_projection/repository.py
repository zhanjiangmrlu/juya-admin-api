from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.user_projection.service import UserProjection
from juya_admin_api.shared.errors import AppError

OPEN_COMPLETIONS = (
    "(SELECT COUNT(DISTINCT lp.scene_id) FROM learning_progress lp "
    "JOIN scene s ON s.public_id=lp.scene_id "
    "JOIN open_scene_item oi ON oi.scene_id=s.id "
    "WHERE lp.user_id=u.id AND lp.completed_at IS NOT NULL "
    "AND oi.config_id=(SELECT MAX(id) FROM open_scene_config))"
)
USER_SELECT = (
    "SELECT u.public_id,u.juya_number,u.status AS account_status,u.last_active_at, "
    "profile.nickname,profile.avatar_object_key,COALESCE(c.change_pending,0) AS change_pending, "
    "COALESCE(c.contact_status,'NOT_PROVIDED') AS contact_status, "
    "(SELECT MAX(h.occurred_at) FROM contact_status_history h WHERE h.user_id=u.id "
    "AND h.note='CONTACT_CHANGED') AS contact_changed_at, "
    f"{OPEN_COMPLETIONS} AS open_scene_completed_count, "
    "(SELECT COUNT(*) FROM formal_entitlement e WHERE e.user_id=u.id) AS formal_entitlement_count, "
    "(SELECT COUNT(*) FROM limited_entitlement e WHERE "
    "e.user_id=u.id) AS limited_entitlement_count, "
    "(SELECT COUNT(*) FROM feedback_ticket f WHERE f.user_id=u.id AND f.status IN "
    "('PENDING','PROCESSING','NEED_MORE','USER_SUPPLIED')) AS open_feedback_count "
    "FROM user_account u LEFT JOIN user_profile profile ON profile.user_id=u.id "
    "LEFT JOIN user_contact c ON c.user_id=u.id "
)


class SQLAlchemyUserProjectionRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        # 功能: 初始化用户投影对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session_factory: 创建 SQLAlchemy 异步会话的工厂,每次操作独立管理事务.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self._session_factory = session_factory

    async def search(
        self,
        query: str | None,
        *,
        user_ids: tuple[str, ...] | None = None,
        contact_status: str | None = None,
        entitlement_type: str | None = None,
        entitlement_status: str | None = None,
        profile_completeness: str | None = None,
        cohort: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[UserProjection, ...]:
        # 功能: 按关键词,联系人状态,权益和资料分群条件分页检索用户.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     query: 用户昵称或句芽号检索词;None 表示不按关键词限制.
        #     user_ids: 用户公开标识集合,限制批量投影查询或搜索范围.
        #     contact_status: 联系人跟进状态筛选条件;None 表示不限.
        #     entitlement_type: 用户权益类型筛选条件,例如 FORMAL 或 LIMITED.
        #     entitlement_status: 用户权益状态筛选条件.
        #     profile_completeness: 用户资料完整度筛选条件,例如 COMPLETE 或 INCOMPLETE.
        #     cohort: 用户分群筛选条件,例如今日新增或开放学习后未留联系方式.
        #     page: 分页页码,从 1 开始,默认第 1 页.
        #     page_size: 每页返回条数,接口范围为 1 至 100,默认 20.
        # 返回: 符合筛选条件和分页范围的用户资料投影.
        if page < 1 or not 1 <= page_size <= 100:
            raise AppError("PAGINATION_INVALID", "分页参数不正确", 422)
        clauses = ["1=1"]
        params: dict[str, object] = {"limit": page_size, "offset": (page - 1) * page_size}
        if query:
            clauses.append(
                "(u.public_id LIKE :query OR u.juya_number LIKE :query OR "
                "profile.nickname LIKE :query)"
            )
            params["query"] = f"%{query}%"
        if user_ids is not None:
            if not user_ids:
                return ()
            placeholders = []
            for index, uid in enumerate(user_ids):
                name = f"uid_{index}"
                placeholders.append(f":{name}")
                params[name] = uid
            clauses.append(f"u.public_id IN ({','.join(placeholders)})")
        if contact_status:
            clauses.append("COALESCE(c.contact_status,'NOT_PROVIDED')=:contact_status")
            params["contact_status"] = contact_status
        if entitlement_type not in {None, "FORMAL", "LIMITED"}:
            raise AppError("USER_FILTER_INVALID", "权益类型不正确", 422)
        if entitlement_status not in {
            None,
            "ACTIVE",
            "PAUSED",
            "REVOKED",
            "PENDING",
            "ENDED",
            "START_EXPIRED",
            "EXPIRED",
        }:
            raise AppError("USER_FILTER_INVALID", "权益状态不正确", 422)
        if entitlement_type or entitlement_status:
            kinds = [entitlement_type.lower()] if entitlement_type else ["formal", "limited"]
            conditions = []
            for kind in kinds:
                effective = (
                    "CASE WHEN e.status IN ('ACTIVE','PAUSED') AND e.expires_at<=:now "
                    "THEN 'EXPIRED' ELSE e.status END"
                    if kind == "formal"
                    else "CASE WHEN e.status='PENDING' AND e.start_deadline<=:now "
                    "THEN 'START_EXPIRED' "
                    "WHEN e.status='ACTIVE' AND e.expires_at<=:now THEN 'ENDED' ELSE e.status END"
                )
                status = f" AND ({effective})=:entitlement_status" if entitlement_status else ""
                conditions.append(
                    f"EXISTS(SELECT 1 FROM {kind}_entitlement e WHERE e.user_id=u.id{status})"
                )
            clauses.append("(" + " OR ".join(conditions) + ")")
            if entitlement_status:
                params["entitlement_status"] = entitlement_status
                params["now"] = datetime.now(UTC)
        if profile_completeness:
            if profile_completeness not in {"COMPLETE", "INCOMPLETE"}:
                raise AppError("USER_FILTER_INVALID", "资料完整度不正确", 422)
            complete = (
                "(NULLIF(profile.nickname,'') IS NOT NULL AND "
                "NULLIF(profile.avatar_object_key,'') IS NOT NULL)"
            )
            clauses.append(complete if profile_completeness == "COMPLETE" else f"NOT {complete}")
        if cohort == "OPEN_WITHOUT_CONTACT":
            clauses.extend(
                [f"{OPEN_COMPLETIONS}>=3", "c.wechat_id_ciphertext IS NULL", "u.status='ACTIVE'"]
            )
        elif cohort == "NEW_TODAY":
            now = datetime.now(UTC)
            start = (now + timedelta(hours=8)).replace(
                hour=0, minute=0, second=0, microsecond=0
            ) - timedelta(hours=8)
            clauses.extend(["u.created_at>=:start", "u.created_at<:end"])
            params.update(start=start, end=start + timedelta(days=1))
        elif cohort is not None:
            raise AppError("USER_FILTER_INVALID", "用户分组不正确", 422)
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        USER_SELECT
                        + "WHERE "
                        + " AND ".join(clauses)
                        + " ORDER BY u.last_active_at DESC,u.id ASC LIMIT :limit OFFSET :offset"
                    ),
                    params,
                )
            ).all()
        return tuple(_from_row(row) for row in rows)

    async def get(self, user_id: str) -> UserProjection | None:
        # 功能: 读取指定用户投影记录,不存在时返回 None.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        # 返回: 匹配的用户资料投影;不存在时为 None.
        async with self._session_factory() as session:
            row = (
                await session.execute(text(USER_SELECT + "WHERE u.public_id=:id"), {"id": user_id})
            ).first()
        return None if row is None else _from_row(row)

    async def records(self, user_id: str) -> dict[str, object]:
        # 功能: 查询用户关联的正式和限时权益,反馈,注销请求及审计操作记录.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        # 返回: 按类别组织的正式权益,限时权益,反馈,注销请求及审计操作记录列表.
        queries = {
            "formal_entitlements": (
                "SELECT e.public_id AS id,p.name,CASE WHEN e.status IN ('ACTIVE','PAUSED') "
                "AND e.expires_at<=:now THEN 'EXPIRED' ELSE e.status END AS status,"
                "e.term,e.granted_at,e.expires_at FROM "
                "formal_entitlement e JOIN content_package p ON p.id=e.package_id "
                "JOIN user_account u ON u.id=e.user_id WHERE u.public_id=:id "
                "ORDER BY e.id DESC"
            ),
            "limited_entitlements": (
                "SELECT e.public_id AS id,c.name,v.public_id AS "
                "campaign_version_id,CASE WHEN e.status='PENDING' AND e.start_deadline<=:now "
                "THEN 'START_EXPIRED' WHEN e.status='ACTIVE' AND e.expires_at<=:now "
                "THEN 'ENDED' ELSE e.status END AS status,e.start_deadline,e.activated_at"
                ",e.expires_at FROM limited_entitlement e JOIN limited_campai"
                "gn_version v ON v.id=e.campaign_version_id JOIN limited_camp"
                "aign c ON c.id=v.campaign_id JOIN user_account u ON u.id=e.u"
                "ser_id WHERE u.public_id=:id ORDER BY e.id DESC"
            ),
            "feedback": (
                "SELECT f.public_id AS "
                "id,f.category,f.description,f.status,f.created_at FROM "
                "feedback_ticket f JOIN user_account u ON u.id=f.user_id WHERE "
                "u.public_id=:id ORDER BY f.id DESC"
            ),
            "deletions": (
                "SELECT d.public_id AS "
                "id,d.status,d.requested_at,d.effective_at,d.completed_at FROM "
                "account_deletion_request d JOIN user_account u ON u.id=d.user_id "
                "WHERE u.public_id=:id ORDER BY d.id DESC"
            ),
            "audit": (
                "SELECT * FROM (SELECT a.public_id AS "
                "id,a.action,a.actor_public_id,a.created_at,a.reason FROM "
                "audit_event a WHERE a.object_public_id=:id OR "
                "JSON_UNQUOTE(JSON_EXTRACT(a.after_summary,'$.user_id'))=:id OR "
                "JSON_UNQUOTE(JSON_EXTRACT(a.before_summary,'$.user_id'))=:id OR "
                "JSON_CONTAINS(JSON_EXTRACT(a.after_summary,'$.user_ids'),JSO"
                "N_QUOTE(:id)) "
                "UNION ALL SELECT o.public_id,CONCAT('formal.',o.operation_type),"
                "o.operator_id,o.created_at,o.reason FROM formal_entitlement_operation o "
                "JOIN formal_entitlement e ON e.id=o.entitlement_id "
                "JOIN user_account u ON u.id=e.user_id WHERE u.public_id=:id "
                "UNION ALL SELECT o.public_id,CONCAT('limited.',o.operation_type),"
                "o.operator_id,o.created_at,o.reason FROM limited_entitlement_operation o "
                "JOIN limited_entitlement e ON e.id=o.entitlement_id "
                "JOIN user_account u ON u.id=e.user_id WHERE u.public_id=:id) history "
                "ORDER BY created_at DESC,id DESC"
            ),
        }
        result: dict[str, object] = {}
        async with self._session_factory() as session:
            for key, query in queries.items():
                rows = (
                    (await session.execute(text(query), {"id": user_id, "now": datetime.now(UTC)}))
                    .mappings()
                    .all()
                )
                result[key] = [
                    {k: _utc(v) if isinstance(v, datetime) else v for k, v in row.items()}
                    for row in rows
                ]
        return result


def _utc(value: datetime | None) -> datetime | None:
    # 功能: 将数据库无时区时间补为 UTC 并保留空值.
    # 参数:
    #     value: 待规范化时区或转换业务日期的时间;None 保留为空.
    # 返回: 规范化日期时间;输入为空或允许空值时为 None.
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def _from_row(row: Any) -> UserProjection:
    # 功能: 将数据库记录转换为用户投影领域对象.
    # 参数:
    #     row: 查询得到的用户投影数据库记录.
    # 返回: 用户资料投影.
    return UserProjection(
        user_id=row.public_id,
        account_status=row.account_status,
        last_active_at=_utc(row.last_active_at),
        formal_entitlement_count=row.formal_entitlement_count,
        limited_entitlement_count=row.limited_entitlement_count,
        open_feedback_count=row.open_feedback_count,
        juya_number=row.juya_number,
        nickname=row.nickname,
        avatar_object_key=row.avatar_object_key,
        open_scene_completed_count=row.open_scene_completed_count,
        change_pending=bool(row.change_pending),
        contact_changed_at=_utc(row.contact_changed_at),
        contact_status=row.contact_status,
    )
