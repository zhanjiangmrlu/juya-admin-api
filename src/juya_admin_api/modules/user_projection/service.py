from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Protocol

from juya_admin_api.integrations.miniapp_api.client import (
    ContactProjection,
    LearningOverview,
    MiniappApiClient,
)
from juya_admin_api.modules.audit.service import AuditEvent, AuditService
from juya_admin_api.shared.errors import AppError

_CONTACT_STATUSES = frozenset(
    {"NOT_PROVIDED", "PENDING", "CONTACTED", "UNREACHABLE", "DO_NOT_CONTACT"}
)


@dataclass(frozen=True, slots=True)
class UserProjection:
    user_id: str
    account_status: str
    last_active_at: datetime | None
    formal_entitlement_count: int
    limited_entitlement_count: int
    open_feedback_count: int
    juya_number: str = ""
    nickname: str | None = None
    avatar_object_key: str | None = None
    open_scene_completed_count: int = 0
    change_pending: bool = False
    contact_changed_at: datetime | None = None
    contact_status: str = "NOT_PROVIDED"
    avatar_url: str | None = None


@dataclass(frozen=True, slots=True)
class UserDetail:
    projection: UserProjection
    contact: ContactProjection | None
    contact_degraded: bool
    learning_overview: LearningOverview | None
    learning_degraded: bool
    records: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class UserListItem:
    projection: UserProjection
    contact: ContactProjection | None
    contact_degraded: bool


class UserProjectionRepository(Protocol):
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
        ...

    async def get(self, user_id: str) -> UserProjection | None:
        # 功能: 读取指定用户投影记录,仓库中不存在时返回 None.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        # 返回: 匹配的用户资料投影;不存在时为 None.
        ...

    async def records(self, user_id: str) -> dict[str, object]:
        # 功能: 定义用户关联权益,反馈,注销和审计操作记录的查询端口.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        # 返回: 按类别组织的正式权益,限时权益,反馈,注销请求及审计操作记录列表.
        ...


class InMemoryUserProjectionRepository:
    def __init__(self) -> None:
        # 功能: 初始化用户投影对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self.users: dict[str, UserProjection] = {}

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
        # 功能: 按用户标识范围,关键词和联系人状态过滤内存用户,再按插入顺序分页.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     query: 用户公开标识,句芽号或昵称的子串;忽略大小写,None 表示不限.
        #     user_ids: 用户公开标识集合,限制批量投影查询或搜索范围.
        #     contact_status: 联系人跟进状态筛选条件;None 表示不限.
        #     entitlement_type: 仓储协议要求的权益类型条件;当前内存实现不使用此值.
        #     entitlement_status: 仓储协议要求的权益状态条件;当前内存实现不使用此值.
        #     profile_completeness: 仓储协议要求的资料完整度条件;当前内存实现不使用此值.
        #     cohort: 仓储协议要求的用户分群条件;当前内存实现不使用此值.
        #     page: 分页页码,从 1 开始,默认第 1 页.
        #     page_size: 每页返回条数,接口范围为 1 至 100,默认 20.
        # 返回: 符合筛选条件和分页范围的用户资料投影.
        allowed = None if user_ids is None else frozenset(user_ids)
        return tuple(
            user
            for user in self.users.values()
            if (allowed is None or user.user_id in allowed)
            and (
                query is None
                or query.lower() in f"{user.user_id} {user.juya_number} {user.nickname}".lower()
            )
            and (contact_status is None or user.contact_status == contact_status)
        )[(page - 1) * page_size : page * page_size]

    async def records(self, user_id: str) -> dict[str, object]:
        # 功能: 为仓储协议提供关联记录空占位,内存实现未存储这些业务记录.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 仓储协议要求的用户公开标识;当前内存实现不使用此值.
        # 返回: 空字典,该内存实现未提供用户关联业务记录.
        return {}

    async def get(self, user_id: str) -> UserProjection | None:
        # 功能: 读取指定用户投影记录,仓库中不存在时返回 None.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        # 返回: 匹配的用户资料投影;不存在时为 None.
        return self.users.get(user_id)


class UserProjectionService:
    def __init__(
        self,
        repository: UserProjectionRepository,
        miniapp_client: MiniappApiClient,
        audit: AuditService,
        *,
        avatar_provider: Callable[[str, str], Awaitable[str | None]] | None = None,
    ) -> None:
        # 功能: 初始化用户投影对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     repository: 提供用户投影持久化和查询能力的仓储.
        #     miniapp_client: 查询小程序用户联系信息和学习概览的内部客户端.
        #     audit: 记录管理员敏感操作的审计服务.
        #     avatar_provider: 按用户标识和头像对象键签发访问地址的异步回调.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self._repository = repository
        self._miniapp_client = miniapp_client
        self._audit = audit
        self._avatar_provider = avatar_provider

    async def search(
        self,
        query: str | None = None,
        *,
        wechat_id: str | None = None,
        contact_status: str | None = None,
        entitlement_type: str | None = None,
        entitlement_status: str | None = None,
        profile_completeness: str | None = None,
        cohort: str | None = None,
        page: int = 1,
        page_size: int = 20,
        admin_id: str,
        request_id: str,
        occurred_at: datetime,
    ) -> tuple[UserListItem, ...]:
        # 功能: 按关键词,联系人状态,权益和资料分群条件分页检索用户.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     query: 用户昵称或句芽号检索词;None 表示不按关键词限制.
        #     wechat_id: 用户提交的微信号,用于精确检索关联用户.
        #     contact_status: 联系人跟进状态筛选条件;None 表示不限.
        #     entitlement_type: 用户权益类型筛选条件,例如 FORMAL 或 LIMITED.
        #     entitlement_status: 用户权益状态筛选条件.
        #     profile_completeness: 用户资料完整度筛选条件,例如 COMPLETE 或 INCOMPLETE.
        #     cohort: 用户分群筛选条件,例如今日新增或开放学习后未留联系方式.
        #     page: 分页页码,从 1 开始,默认第 1 页.
        #     page_size: 每页返回条数,接口范围为 1 至 100,默认 20.
        #     admin_id: 执行本次操作的管理员公开标识.
        #     request_id: 本次 HTTP 请求的关联标识,串联日志与审计.
        #     occurred_at: 业务事件或审计记录的发生时间.
        # 返回: 合并联系信息和可用状态后的用户列表.
        if contact_status is not None and contact_status not in _CONTACT_STATUSES:
            raise AppError("CONTACT_STATUS_INVALID", "联系状态无效", 422)
        user_ids = None
        if wechat_id is not None:
            user_ids = await self._miniapp_client.search_user_ids_by_wechat(wechat_id, admin_id)
        projections = await self._repository.search(
            query,
            user_ids=user_ids,
            contact_status=contact_status,
            entitlement_type=entitlement_type,
            entitlement_status=entitlement_status,
            profile_completeness=profile_completeness,
            cohort=cohort,
            page=page,
            page_size=page_size,
        )
        if not projections:
            return ()
        contacts = await self._miniapp_client.get_contact_projections(
            tuple(item.user_id for item in projections), admin_id
        )
        if contacts.degraded and contact_status is not None:
            raise AppError("MINIAPP_API_UNAVAILABLE", "联系资料服务暂不可用", 503)
        by_user_id = {item.user_id: item for item in contacts.contacts}
        projections = tuple([await self._with_avatar(projection) for projection in projections])
        items = tuple(
            UserListItem(projection, by_user_id.get(projection.user_id), contacts.degraded)
            for projection in projections
        )
        sensitive_ids = [
            item.projection.user_id
            for item in items
            if item.contact is not None and item.contact.wechat_id is not None
        ]
        if sensitive_ids:
            await self._audit.record(
                AuditEvent(
                    actor_public_id=admin_id,
                    action="contact.view.list",
                    object_type="user_contact",
                    object_public_id="users",
                    before_summary={},
                    after_summary={"hit_count": len(sensitive_ids), "user_ids": sensitive_ids},
                    reason=None,
                    request_id=request_id,
                    occurred_at=occurred_at,
                )
            )
        return items

    async def _with_avatar(self, projection: UserProjection) -> UserProjection:
        # 功能: 为用户投影补充规范路径下的头像签名地址.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     projection: 从数据库或内部服务获得的用户信息投影.
        # 返回: 用户资料投影.
        if projection.avatar_object_key and self._avatar_provider:
            return replace(
                projection,
                avatar_url=await self._avatar_provider(
                    projection.user_id, projection.avatar_object_key
                ),
            )
        return projection

    async def contacts_for(
        self,
        user_ids: tuple[str, ...],
        *,
        admin_id: str,
        occurred_at: datetime,
    ) -> tuple[dict[str, ContactProjection], bool]:
        # 功能: 批量查询用户联系投影,上游不可用时返回明确降级状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_ids: 用户公开标识集合,限制批量投影查询或搜索范围.
        #     admin_id: 执行本次操作的管理员公开标识.
        #     occurred_at: 业务事件或审计记录的发生时间.
        # 返回: 用户标识到联系投影的映射,以及上游是否不可用的标记.
        result = await self._miniapp_client.get_contact_projections(user_ids, admin_id)
        by_id = {contact.user_id: contact for contact in result.contacts}
        sensitive = [c.user_id for c in result.contacts if c.wechat_id is not None]
        if sensitive:
            await self._audit.record(
                AuditEvent(
                    actor_public_id=admin_id,
                    action="contact.view.entitlements",
                    object_type="user_contact",
                    object_public_id="entitlements",
                    before_summary={},
                    after_summary={"hit_count": len(sensitive), "user_ids": sensitive},
                    reason=None,
                    request_id="entitlement-list",
                    occurred_at=occurred_at,
                )
            )
        return by_id, result.degraded

    async def detail(
        self,
        user_id: str,
        *,
        admin_id: str,
        request_id: str,
        occurred_at: datetime,
    ) -> UserDetail:
        # 功能: 查询用户详情和联系投影,合并学习概览并记录访问审计.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     admin_id: 执行本次操作的管理员公开标识.
        #     request_id: 本次 HTTP 请求的关联标识,串联日志与审计.
        #     occurred_at: 业务事件或审计记录的发生时间.
        # 返回: 用户投影,联系信息及学习记录组成的详情.
        projection = await self._repository.get(user_id)
        if projection is None:
            raise AppError("USER_NOT_FOUND", "用户不存在", 404)
        projection = await self._with_avatar(projection)
        contacts = await self._miniapp_client.get_contact_projections((user_id,), admin_id)
        contact = next((item for item in contacts.contacts if item.user_id == user_id), None)
        learning_degraded = False
        try:
            learning = await self._miniapp_client.get_learning_overview(user_id, admin_id)
        except AppError as error:
            if error.code != "MINIAPP_API_UNAVAILABLE":
                raise
            learning = None
            learning_degraded = True
        if contact is not None and contact.wechat_id is not None:
            await self._audit.record(
                AuditEvent(
                    actor_public_id=admin_id,
                    action="contact.view.detail",
                    object_type="user_contact",
                    object_public_id=user_id,
                    before_summary={},
                    after_summary={"hit_count": 1, "user_id": user_id},
                    reason=None,
                    request_id=request_id,
                    occurred_at=occurred_at,
                )
            )
        return UserDetail(
            projection,
            contact,
            contacts.degraded,
            learning,
            learning_degraded,
            await self._repository.records(user_id),
        )
