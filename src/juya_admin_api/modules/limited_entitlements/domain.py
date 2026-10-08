from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from juya_admin_api.shared.errors import AppError


class RemedyMode(StrEnum):
    EXTEND_START_DEADLINE = "EXTEND_START_DEADLINE"
    RESTORE_START_WINDOW = "RESTORE_START_WINDOW"


@dataclass(slots=True)
class LimitedEntitlement:
    id: str
    user_id: str
    campaign_version_id: str
    status: str
    granted_at: datetime
    start_deadline: datetime
    duration_days: int
    activation_window_days: int
    scene_ids: tuple[str, ...]
    activated_at: datetime | None = None
    expires_at: datetime | None = None
    remedy_count: int = 0
    version: int = 1


def calculate_remedy(
    current: LimitedEntitlement,
    mode: RemedyMode,
    now: datetime,
) -> LimitedEntitlement:
    # 功能: 计算限时权益的一次性启动窗口补救结果.
    # 参数:
    #     current: 操作前的权益状态;可空类型允许尚未开通.
    #     mode: 限时权益启动窗口补救模式;None 表示当前命令无补救模式.
    #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
    # 返回: 计算或持久化后的限时权益状态.
    if current.status == "ACTIVE":
        raise AppError("LIMITED_ACTIVE_CANNOT_EXTEND", "已激活权益不可延长或重置", 409)
    if current.remedy_count >= 1:
        raise AppError("LIMITED_REMEDY_ALREADY_USED", "该权益已使用过一次补救", 409)
    if mode is RemedyMode.EXTEND_START_DEADLINE and current.status == "PENDING":
        start_deadline = current.start_deadline + timedelta(days=current.activation_window_days)
        return replace(
            current,
            start_deadline=start_deadline,
            remedy_count=1,
            version=current.version + 1,
        )
    if mode is RemedyMode.RESTORE_START_WINDOW and current.status == "START_EXPIRED":
        return replace(
            current,
            status="PENDING",
            start_deadline=now + timedelta(days=current.activation_window_days),
            remedy_count=1,
            version=current.version + 1,
        )
    raise AppError("LIMITED_REMEDY_STATE_CONFLICT", "当前状态不允许该补救方式", 409)


def calculate_pause(current: LimitedEntitlement, now: datetime) -> LimitedEntitlement:
    # 功能: 校验限时权益仍然有效并计算暂停状态.
    # 参数:
    #     current: 操作前的权益状态;可空类型允许尚未开通.
    #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
    # 返回: 计算或持久化后的限时权益状态.
    if current.status != "ACTIVE" or (current.expires_at is not None and now >= current.expires_at):
        raise AppError("LIMITED_STATE_CONFLICT", "当前状态不可暂停", 409)
    return replace(current, status="PAUSED", version=current.version + 1)


def calculate_resume(current: LimitedEntitlement, now: datetime) -> LimitedEntitlement:
    # 功能: 校验限时权益未到期并计算恢复状态.
    # 参数:
    #     current: 操作前的权益状态;可空类型允许尚未开通.
    #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
    # 返回: 计算或持久化后的限时权益状态.
    if current.status != "PAUSED":
        raise AppError("LIMITED_STATE_CONFLICT", "当前状态不可恢复", 409)
    if current.expires_at is not None and now >= current.expires_at:
        raise AppError("LIMITED_ENTITLEMENT_EXPIRED", "限时权益已到期", 409)
    return replace(current, status="ACTIVE", version=current.version + 1)


def calculate_revoke(current: LimitedEntitlement) -> LimitedEntitlement:
    # 功能: 校验限时权益可撤销并计算撤销状态.
    # 参数:
    #     current: 操作前的权益状态;可空类型允许尚未开通.
    # 返回: 计算或持久化后的限时权益状态.
    if current.status not in {"PENDING", "PAUSED", "START_EXPIRED"}:
        raise AppError("LIMITED_STATE_CONFLICT", "当前状态不可撤销", 409)
    return replace(current, status="REVOKED", version=current.version + 1)
