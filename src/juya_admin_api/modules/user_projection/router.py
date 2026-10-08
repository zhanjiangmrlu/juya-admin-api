from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import UTC, date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.integrations.miniapp_api.client import ContactProjection
from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.analytics.service import (
    AnalyticsRepository,
    export_aggregate_rows,
)
from juya_admin_api.modules.dashboard.service import DashboardService
from juya_admin_api.modules.user_projection.deletion_service import DeletionCleanupService
from juya_admin_api.modules.user_projection.service import (
    UserListItem,
    UserProjection,
    UserProjectionService,
)
from juya_admin_api.modules.work_items.service import WorkItemService


class WechatSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wechat_id: str = Field(min_length=1, max_length=64)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    contact_status: str | None = None
    entitlement_type: Literal["FORMAL", "LIMITED"] | None = None
    entitlement_status: str | None = None
    profile_completeness: Literal["COMPLETE", "INCOMPLETE"] | None = None
    cohort: Literal["NEW_TODAY", "OPEN_WITHOUT_CONTACT"] | None = None


class DeletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=128)


class AccountDeletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=26, max_length=26)
    deletion_request_id: str = Field(min_length=26, max_length=26)
    event_id: str = Field(min_length=26, max_length=26)


AdminDependency = Callable[..., Awaitable[SessionRecord]]
ContactStatus = Literal[
    "NOT_PROVIDED",
    "PENDING",
    "CONTACTED",
    "UNREACHABLE",
    "DO_NOT_CONTACT",
]


class UserContactResponse(BaseModel):
    wechat_id: str | None
    contact_status: ContactStatus
    change_pending: bool
    verified_at: datetime | None
    verified_by: str | None
    updated_at: datetime


class UserProjectionResponse(BaseModel):
    user_id: str
    account_status: str
    last_active_at: datetime | None
    formal_entitlement_count: int
    limited_entitlement_count: int
    open_feedback_count: int
    contact: UserContactResponse | None
    contact_degraded: bool
    juya_number: str = ""
    nickname: str | None = None
    avatar_object_key: str | None = None
    avatar_url: str | None = None
    open_scene_completed_count: int | None = 0
    change_pending: bool = False
    contact_changed_at: datetime | None = None


class UserDetailResponse(UserProjectionResponse):
    learning_degraded: bool
    open_scene_completed_count: int | None
    learning_days: int | None
    favorite_count: int | None
    records: dict[str, object] = Field(default_factory=dict)


def _request_id(request: Request) -> str:
    # 功能: 提取请求上下文中关联日志和审计的请求标识.
    # 参数:
    #     request: 当前 HTTP 请求,提供头部,路径,请求体及请求关联上下文.
    # 返回: 请求上下文中的关联标识;上下文缺失时返回 unknown.
    return str(getattr(request.state, "request_id", "unknown"))


def _no_store(response: Response) -> None:
    # 功能: 设置禁止缓存响应的头部,保护敏感运营数据.
    # 参数:
    #     response: 当前 HTTP 响应,用于设置 Cookie,禁止缓存等头部.
    # 返回: 无返回值;正常完成表示本次操作成功.
    response.headers["Cache-Control"] = "no-store"


# 匿名函数: 为业务服务提供可注入的 UTC 当前时钟.
# 参数: 无.
# 返回: 当前带 UTC 时区的日期时间.
def create_operations_router(
    users: UserProjectionService,
    dashboard: DashboardService,
    work_items: WorkItemService,
    analytics: AnalyticsRepository,
    *,
    current_admin: AdminDependency,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    # 功能: 创建运营用户搜索,详情,首页,待办和统计导出路由.
    # 参数:
    #     users: 用户搜索,详情和联系人投影服务.
    #     dashboard: 生成运营首页指标快照的服务.
    #     work_items: 生成并排序当前运营待办的服务.
    #     analytics: 匿名统计数据查询仓储.
    #     current_admin: 注入已认证管理员会话的只读依赖.
    #     clock: 返回当前带时区时间的回调,便于控制签名和业务时间.
    # 返回: 已注册业务端点的 FastAPI 路由对象.
    router = APIRouter(prefix="/api/v1/admin", tags=["admin-operations"])

    @router.get("/users", response_model=list[UserProjectionResponse])
    async def search_users(
        request: Request,
        response: Response,
        admin: Annotated[SessionRecord, Depends(current_admin)],
        query: Annotated[str | None, Query(max_length=64)] = None,
        contact_status: Annotated[ContactStatus | None, Query()] = None,
        entitlement_type: Literal["FORMAL", "LIMITED"] | None = None,
        entitlement_status: Literal[
            "ACTIVE", "PAUSED", "REVOKED", "PENDING", "ENDED", "START_EXPIRED", "EXPIRED"
        ]
        | None = None,
        profile_completeness: Literal["COMPLETE", "INCOMPLETE"] | None = None,
        cohort: Literal["NEW_TODAY", "OPEN_WITHOUT_CONTACT"] | None = None,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> list[dict[str, object]]:
        # 功能: 按运营筛选条件分页检索用户并禁止缓存敏感结果.
        # 参数:
        #     request: 当前 HTTP 请求,提供头部,路径,请求体及请求关联上下文.
        #     response: 当前 HTTP 响应,用于设置 Cookie,禁止缓存等头部.
        #     admin: 经认证且按接口要求完成 CSRF 校验的管理员会话.
        #     query: 用户昵称或句芽号检索词;None 表示不按关键词限制.
        #     contact_status: 联系人跟进状态筛选条件;None 表示不限.
        #     entitlement_type: 用户权益类型筛选条件,例如 FORMAL 或 LIMITED.
        #     entitlement_status: 用户权益状态筛选条件.
        #     profile_completeness: 用户资料完整度筛选条件,例如 COMPLETE 或 INCOMPLETE.
        #     cohort: 用户分群筛选条件,例如今日新增或开放学习后未留联系方式.
        #     page: 分页页码,从 1 开始,默认第 1 页.
        #     page_size: 每页返回条数,接口范围为 1 至 100,默认 20.
        # 返回: 当前页用户资料与联系投影的列表,各项包含联系服务降级标记.
        _no_store(response)
        items = await users.search(
            query,
            contact_status=contact_status,
            entitlement_type=entitlement_type,
            entitlement_status=entitlement_status,
            profile_completeness=profile_completeness,
            cohort=cohort,
            page=page,
            page_size=page_size,
            admin_id=str(admin.admin_user_id),
            request_id=_request_id(request),
            occurred_at=clock(),
        )
        return [_list_item_body(item) for item in items]

    @router.post("/users/search-by-wechat", response_model=list[UserProjectionResponse])
    async def search_users_by_wechat(
        payload: WechatSearchRequest,
        request: Request,
        response: Response,
        admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> list[dict[str, object]]:
        # 功能: 按微信号检索用户并记录敏感查询审计.
        # 参数:
        #     payload: 要检索的微信号.
        #     request: 当前 HTTP 请求,提供头部,路径,请求体及请求关联上下文.
        #     response: 当前 HTTP 响应,用于设置 Cookie,禁止缓存等头部.
        #     admin: 经认证且按接口要求完成 CSRF 校验的管理员会话.
        # 返回: 匹配微信号并满足附加筛选的用户资料与联系投影列表,附带降级标记.
        _no_store(response)
        items = await users.search(
            wechat_id=payload.wechat_id,
            contact_status=payload.contact_status,
            entitlement_type=payload.entitlement_type,
            entitlement_status=payload.entitlement_status,
            profile_completeness=payload.profile_completeness,
            cohort=payload.cohort,
            page=payload.page,
            page_size=payload.page_size,
            admin_id=str(admin.admin_user_id),
            request_id=_request_id(request),
            occurred_at=clock(),
        )
        return [_list_item_body(item) for item in items]

    @router.get("/users/{user_id}", response_model=UserDetailResponse)
    async def user_detail(
        user_id: str,
        request: Request,
        response: Response,
        admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, object]:
        # 功能: 查询用户详情,联系信息及学习概览并记录访问审计.
        # 参数:
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     request: 当前 HTTP 请求,提供头部,路径,请求体及请求关联上下文.
        #     response: 当前 HTTP 响应,用于设置 Cookie,禁止缓存等头部.
        #     admin: 经认证且按接口要求完成 CSRF 校验的管理员会话.
        # 返回: 用户资料及联系投影,学习计数,关联记录,并包含联系和学习服务的降级标记.
        _no_store(response)
        detail = await users.detail(
            user_id,
            admin_id=str(admin.admin_user_id),
            request_id=_request_id(request),
            occurred_at=clock(),
        )
        body = _projection_body(detail.projection)
        body["contact_degraded"] = detail.contact_degraded
        body["contact"] = _contact_body(detail.contact)
        body["learning_degraded"] = detail.learning_degraded
        learning = detail.learning_overview
        body["open_scene_completed_count"] = (
            None if learning is None else learning.open_scene_completed_count
        )
        body["learning_days"] = None if learning is None else learning.learning_days
        body["favorite_count"] = None if learning is None else learning.favorite_count
        body["records"] = detail.records
        return body

    @router.get("/dashboard")
    async def dashboard_snapshot(
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, int]:
        # 功能: 返回运营首页各项汇总指标.
        # 参数:
        #     _admin: 认证依赖注入的管理员会话,仅用于执行访问校验.
        # 返回: 用户,联系跟进,反馈,限时权益,临期权益和失败任务计数,以及预警天数配置.
        snapshot = await dashboard.get_snapshot()
        return asdict(snapshot)

    @router.get("/work-items")
    async def active_work_items(
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> list[dict[str, object]]:
        # 功能: 返回按业务优先级排序的当前运营待办.
        # 参数:
        #     _admin: 认证依赖注入的管理员会话,仅用于执行访问校验.
        # 返回: 按业务优先级排序的待办列表,每项含 key,kind,priority_rank,due_at.
        return [
            {
                "key": item.key,
                "kind": item.kind,
                "priority_rank": item.priority_rank,
                "due_at": item.due_at,
            }
            for item in await work_items.active(clock())
        ]

    @router.get("/analytics/export")
    async def analytics_export(
        start: date,
        end: date,
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> tuple[dict[str, object], ...]:
        # 功能: 导出指定日期范围内的匿名聚合统计.
        # 参数:
        #     start: 统计查询的起始日期,包含当日.
        #     end: 统计查询的结束日期,包含当日.
        #     _admin: 认证依赖注入的管理员会话,仅用于执行访问校验.
        # 返回: 每项含统计日期,指标名,匿名维度和计数的导出记录元组.
        return export_aggregate_rows(await analytics.query(start, end))

    return router


# 匿名函数: 为业务服务提供可注入的 UTC 当前时钟.
# 参数: 无.
# 返回: 当前带 UTC 时区的日期时间.
def create_internal_deletion_router(
    service: DeletionCleanupService,
    *,
    current_service: Callable[..., Awaitable[object]],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    # 功能: 创建用户注销关联数据清理内部路由.
    # 参数:
    #     service: 执行业务操作的用户投影服务.
    #     current_service: 校验内部服务请求签名并注入服务身份的依赖.
    #     clock: 返回当前带时区时间的回调,便于控制签名和业务时间.
    # 返回: 已注册业务端点的 FastAPI 路由对象.
    router = APIRouter(prefix="/internal/v1", tags=["internal-users"])

    @router.post("/account-deletions")
    async def cleanup_account(
        payload: AccountDeletionRequest,
        _principal: Annotated[object, Depends(current_service)],
    ) -> dict[str, object]:
        # 功能: 按注销请求清理账户关联数据并返回处理摘要.
        # 参数:
        #     payload: 用户标识,注销请求标识及唯一事件标识.
        #     _principal: 内部签名校验后注入的调用服务身份.
        # 返回: 清理事件 event_id,用户 user_id,处理 status 和 completed_at 完成时间.
        result = await service.cleanup(
            payload.event_id,
            payload.user_id,
            clock(),
            deletion_request_id=payload.deletion_request_id,
        )
        return {
            "event_id": result.event_id,
            "user_id": result.user_id,
            "status": result.status,
            "completed_at": result.completed_at,
        }

    @router.post("/users/{user_id}/deletion")
    async def cleanup_user(
        user_id: str,
        payload: DeletionRequest,
        _principal: Annotated[object, Depends(current_service)],
    ) -> dict[str, object]:
        # 功能: 按用户标识处理内部注销清理请求.
        # 参数:
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     payload: 注销清理事件及可选注销请求标识.
        #     _principal: 内部签名校验后注入的调用服务身份.
        # 返回: 清理事件 event_id,用户 user_id,处理 status 和 completed_at 完成时间.
        result = await service.cleanup(payload.event_id, user_id, clock())
        return {
            "event_id": result.event_id,
            "user_id": result.user_id,
            "status": result.status,
            "completed_at": result.completed_at,
        }

    return router


def _projection_body(projection: UserProjection) -> dict[str, object]:
    # 功能: 把用户投影转换为接口响应字段.
    # 参数:
    #     projection: 从数据库或内部服务获得的用户信息投影.
    # 返回: 用户公开标识,昵称,状态,时间等投影字段.
    return {
        "user_id": projection.user_id,
        "juya_number": projection.juya_number,
        "nickname": projection.nickname,
        "avatar_object_key": projection.avatar_object_key,
        "avatar_url": projection.avatar_url,
        "open_scene_completed_count": projection.open_scene_completed_count,
        "change_pending": projection.change_pending,
        "contact_changed_at": projection.contact_changed_at,
        "account_status": projection.account_status,
        "last_active_at": projection.last_active_at,
        "formal_entitlement_count": projection.formal_entitlement_count,
        "limited_entitlement_count": projection.limited_entitlement_count,
        "open_feedback_count": projection.open_feedback_count,
    }


def _contact_body(contact: ContactProjection | None) -> dict[str, object] | None:
    # 功能: 把联系信息投影转换为接口响应.
    # 参数:
    #     contact: 从小程序服务获得的联系人信息投影.
    # 返回: 联系人投影的展示字段,联系信息为空时返回 None.
    if contact is None:
        return None
    return {
        "wechat_id": contact.wechat_id,
        "contact_status": contact.contact_status,
        "change_pending": contact.change_pending,
        "verified_at": contact.verified_at,
        "verified_by": contact.verified_by,
        "updated_at": contact.updated_at,
    }


def _list_item_body(item: UserListItem) -> dict[str, object]:
    # 功能: 组合用户投影,联系信息及降级状态为列表响应.
    # 参数:
    #     item: 待转换为接口响应的用户列表项.
    # 返回: 用户展示信息,联系投影及上游可用状态.
    body = _projection_body(item.projection)
    body["contact"] = _contact_body(item.contact)
    body["contact_degraded"] = item.contact_degraded
    return body
