from datetime import datetime

from juya_admin_api.modules.access_policy.domain import AccessDecision, AccessLevel
from juya_admin_api.modules.limited_entitlements.domain import (
    LimitedEntitlement,
    RemedyMode,
    calculate_pause,
    calculate_remedy,
    calculate_resume,
    calculate_revoke,
)
from juya_admin_api.modules.limited_entitlements.repository import (
    LimitedEntitlementRepository,
)


class LimitedEntitlementService:
    def __init__(self, repository: LimitedEntitlementRepository) -> None:
        # 功能: 初始化限时权益对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     repository: 提供限时权益持久化和查询能力的仓储.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self._repository = repository

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
        return await self._repository.grant(
            user_id, campaign_version_id, actor_id, idempotency_key, now
        )

    async def activate_for_scene(
        self, user_id: str, scene_id: str, now: datetime
    ) -> AccessDecision:
        # 功能: 首次访问活动场景时激活对应限时权益.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     scene_id: 学习场景公开标识.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 场景访问级别,授权来源及最早到期边界.
        entitlement = await self._repository.activate_for_scene(user_id, scene_id, now)
        if (
            entitlement is None
            or entitlement.status != "ACTIVE"
            or entitlement.expires_at is None
            or now >= entitlement.expires_at
        ):
            return AccessDecision(AccessLevel.HIDDEN, (), None)
        return AccessDecision(
            AccessLevel.LIMITED,
            (f"LIMITED:{entitlement.id}",),
            entitlement.expires_at,
            entitlement.activated_at,
        )

    async def remedy(
        self,
        entitlement_id: str,
        mode: RemedyMode,
        actor_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> LimitedEntitlement:
        # 功能: 执行限时权益启动期限延长或启动窗口恢复.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     entitlement_id: 待查询或变更的权益标识.
        #     mode: 限时权益启动窗口补救模式;None 表示当前命令无补救模式.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 计算或持久化后的限时权益状态.
        # 匿名函数: 按启动窗口补救模式计算新的限时权益.
        # 参数:
        #     current: 等待命令计算的当前限时权益状态.
        # 返回: 校验补救次数及状态后的限时权益.
        return await self._repository.change(
            entitlement_id,
            actor_id,
            idempotency_key,
            "REMEDY",
            now,
            lambda current: calculate_remedy(current, mode, now),
        )

    async def pause(
        self, entitlement_id: str, actor_id: str, idempotency_key: str, reason: str, now: datetime
    ) -> LimitedEntitlement:
        # 功能: 暂停仍在有效期内的限时权益.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     entitlement_id: 待查询或变更的权益标识.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     reason: 业务状态变更的原因说明,供校验和审计记录.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 计算或持久化后的限时权益状态.
        # 匿名函数: 计算当前限时权益暂停后的状态.
        # 参数:
        #     current: 等待命令计算的当前限时权益状态.
        # 返回: 暂停后且版本递增的限时权益.
        return await self._repository.change(
            entitlement_id,
            actor_id,
            idempotency_key,
            "PAUSE",
            now,
            lambda current: calculate_pause(current, now),
            reason=reason,
        )

    async def resume(
        self, entitlement_id: str, actor_id: str, idempotency_key: str, now: datetime
    ) -> LimitedEntitlement:
        # 功能: 恢复尚未到期的已暂停限时权益.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     entitlement_id: 待查询或变更的权益标识.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 计算或持久化后的限时权益状态.
        # 匿名函数: 计算当前限时权益恢复后的状态.
        # 参数:
        #     current: 等待命令计算的当前限时权益状态.
        # 返回: 恢复激活且版本递增的限时权益.
        return await self._repository.change(
            entitlement_id,
            actor_id,
            idempotency_key,
            "RESUME",
            now,
            lambda current: calculate_resume(current, now),
        )

    async def revoke(
        self, entitlement_id: str, actor_id: str, idempotency_key: str, reason: str, now: datetime
    ) -> LimitedEntitlement:
        # 功能: 撤销允许撤销的限时权益并记录原因.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     entitlement_id: 待查询或变更的权益标识.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     reason: 业务状态变更的原因说明,供校验和审计记录.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 计算或持久化后的限时权益状态.
        return await self._repository.change(
            entitlement_id,
            actor_id,
            idempotency_key,
            "REVOKE",
            now,
            calculate_revoke,
            reason=reason,
        )
