from datetime import datetime
from typing import Protocol

from juya_admin_api.modules.access_policy.domain import (
    AccessDecision,
    AccessGrant,
    AccessLevel,
)


class ContentAccessPort(Protocol):
    async def is_open(self, scene_id: str, now: datetime) -> bool:
        # 功能: 判断场景在当前时间是否属于开放内容.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     scene_id: 学习场景公开标识.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 条件成立或操作成功时为 True,否则为 False.
        ...

    async def is_preview(self, scene_id: str, now: datetime) -> bool:
        # 功能: 判断场景在当前时间是否允许预览.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     scene_id: 学习场景公开标识.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 条件成立或操作成功时为 True,否则为 False.
        ...


class FormalGrantPort(Protocol):
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
        ...


class LimitedGrantPort(Protocol):
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
        ...


class AccessPolicyService:
    def __init__(
        self,
        content: ContentAccessPort,
        formal_grants: FormalGrantPort,
        limited_grants: LimitedGrantPort,
    ) -> None:
        # 功能: 初始化场景访问授权对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     content: 查询场景开放和预览策略的内容访问端口.
        #     formal_grants: 查询用户对场景拥有的正式权益授权端口.
        #     limited_grants: 查询用户对场景拥有的限时权益授权端口.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self._content = content
        self._formal_grants = formal_grants
        self._limited_grants = limited_grants

    async def authorize(self, user_id: str, scene_id: str, now: datetime) -> AccessDecision:
        # 功能: 合并开放,正式和限时授权,决定场景访问级别及到期边界.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     scene_id: 学习场景公开标识.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 场景访问级别,授权来源及最早到期边界.
        opened = await self._content.is_open(scene_id, now)
        formal = self._active(await self._formal_grants.active_grants(user_id, scene_id, now), now)
        limited = self._active(
            await self._limited_grants.active_grants(user_id, scene_id, now), now
        )

        if opened or formal or limited:
            sources = (
                (("OPEN",) if opened else ())
                + tuple(grant.source for grant in formal)
                + tuple(grant.source for grant in limited)
            )
            expiries = tuple(
                grant.expires_at for grant in (*formal, *limited) if grant.expires_at is not None
            )
            level = (
                AccessLevel.OPEN
                if opened
                else AccessLevel.FORMAL
                if formal
                else AccessLevel.LIMITED
            )
            return AccessDecision(
                level,
                sources,
                min(expiries) if expiries else None,
            )

        if await self._content.is_preview(scene_id, now):
            return AccessDecision(AccessLevel.PREVIEW, ("PREVIEW",), None)
        return AccessDecision(AccessLevel.HIDDEN, (), None)

    @staticmethod
    def _active(grants: tuple[AccessGrant, ...], now: datetime) -> tuple[AccessGrant, ...]:
        # 功能: 过滤已经到期的场景访问授权.
        # 参数:
        #     grants: 待过滤的场景访问授权集合.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 符合用户,场景和时效条件的访问授权集合.
        return tuple(
            grant for grant in grants if grant.expires_at is None or now < grant.expires_at
        )
