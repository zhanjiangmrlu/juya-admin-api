"""Transactional event adapters for existing entitlement and feedback state machines."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from juya_admin_api.modules.analytics.events import append_event
from juya_admin_api.modules.limited_entitlements.domain import LimitedEntitlement


def business_day(value: datetime) -> str:
    # 功能: 将事件时间转换为北京时间业务日期字符串.
    # 参数:
    #     value: 待规范化时区或转换业务日期的时间.
    # 返回: 北京时间日期,格式为 YYYY-MM-DD.
    return (
        value.replace(tzinfo=UTC).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        if value.tzinfo is None
        else value.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    )


async def limited_event(
    session: AsyncSession, entitlement: LimitedEntitlement, event_type: str, now: datetime
) -> None:
    # 功能: 写入限时权益状态变更的匿名统计事件.
    # 参数:
    #     session: 当前 SQLAlchemy 异步数据库会话,在调用方事务内执行读写.
    #     entitlement: 当前权益领域对象,提供状态,期限和业务关联信息.
    #     event_type: 业务事件类型,必须符合该事件的维度及载荷约束.
    #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
    # 返回: 无返回值;正常完成表示本次操作成功.
    user_id = await session.scalar(
        text("SELECT id FROM user_account WHERE public_id=:id"), {"id": entitlement.user_id}
    )
    campaign_id = await session.scalar(
        text(
            "SELECT c.public_id FROM limited_entitlement le "
            "JOIN limited_campaign_version cv ON cv.id=le.campaign_version_id "
            "JOIN limited_campaign c ON c.id=cv.campaign_id WHERE le.public_id=:id"
        ),
        {"id": entitlement.id},
    )
    key = f"limited:{entitlement.id}:{event_type}"
    if event_type == "LIMITED_STATUS_CHANGED":
        key += f":{entitlement.version}"
    await append_event(
        session,
        event_key=key,
        event_type=event_type,
        user_id=user_id,
        occurred_at=now,
        dimension=f"campaign:{campaign_id}" if campaign_id else f"MODE_{entitlement.duration_days}",
        payload={
            "mode": entitlement.duration_days,
            "status": entitlement.status,
            "cohort_day": business_day(entitlement.granted_at),
            "started_day": business_day(entitlement.activated_at or entitlement.granted_at),
        },
    )


async def feedback_event(
    session: AsyncSession,
    ticket_id: int,
    timeline_type: str,
    actor_type: str,
    now: datetime,
    response_deadline: datetime | None = None,
) -> None:
    # 功能: 将反馈时间线变化转换为匿名统计事件.
    # 参数:
    #     session: 当前 SQLAlchemy 异步数据库会话,在调用方事务内执行读写.
    #     ticket_id: 反馈工单数据库数值主键.
    #     timeline_type: 反馈时间线事件类型.
    #     actor_type: 执行操作的主体类型,例如 ADMIN 或 USER.
    #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
    #     response_deadline: 反馈首次响应截止时间;None 时无对应 SLA 截止值.
    # 返回: 无返回值;正常完成表示本次操作成功.
    row = (
        await session.execute(
            text(
                "SELECT user_id,category,status,created_at,supplement_rounds,reopen_count "
                "FROM feedback_ticket WHERE id=:id"
            ),
            {"id": ticket_id},
        )
    ).one()
    types = {
        "CREATED": "FEEDBACK_CREATED",
        "USER_SUPPLIED": "FEEDBACK_SUPPLEMENTED",
        "RESOLVED": "FEEDBACK_RESOLVED",
        "REOPENED": "FEEDBACK_REOPENED",
    }
    created = row.created_at.replace(tzinfo=UTC)
    payload: dict[str, object] = {
        "status": row.status,
        "category": row.category,
        "created_day": business_day(created),
        "supplement_rounds": row.supplement_rounds,
    }
    event_type = types.get(timeline_type, "FEEDBACK_STATUS_CHANGED")
    key = f"feedback:{ticket_id}:{event_type}"
    if event_type not in {"FEEDBACK_CREATED", "FEEDBACK_RESOLVED", "FEEDBACK_REOPENED"}:
        key += f":{timeline_type}:{row.supplement_rounds}:{row.reopen_count}"
    await append_event(
        session,
        event_key=key,
        event_type=event_type,
        user_id=row.user_id,
        occurred_at=now,
        dimension=row.category,
        payload=payload,
    )
    if actor_type == "ADMIN" and timeline_type in {"PROCESSING_STARTED", "NEED_MORE", "RESOLVED"}:
        await append_event(
            session,
            event_key=f"feedback-first-response:{ticket_id}",
            event_type="FEEDBACK_RESPONDED",
            user_id=row.user_id,
            occurred_at=now,
            dimension=row.category,
            payload={
                **payload,
                "response_seconds": max(0, int((now.astimezone(UTC) - created).total_seconds())),
                "before_expiry": response_deadline is not None and now <= response_deadline,
            },
        )
