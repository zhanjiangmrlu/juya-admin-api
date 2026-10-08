from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.infrastructure.security.service_hmac import ServicePrincipal
from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.audit.service import AuditEvent, AuditService
from juya_admin_api.modules.feedback.domain import (
    FeedbackAdminDetail,
    FeedbackAdminPage,
    FeedbackTicket,
)
from juya_admin_api.modules.feedback.service import FeedbackService
from juya_admin_api.modules.media.service import MediaService
from juya_admin_api.shared.errors import AppError


class FeedbackCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(min_length=1, max_length=64)
    category: str
    description: str = Field(min_length=1, max_length=300)
    source: dict[str, Any] = Field(default_factory=dict)
    screenshots: list[str] = Field(default_factory=list, max_length=1)


class SupplementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=300)
    screenshots: list[str] = Field(default_factory=list, max_length=1)


class UserFeedbackQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(min_length=1, max_length=64)


class UserResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(min_length=1, max_length=64)
    action: str
    reason: str | None = Field(default=None, max_length=300)


class SupplementCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_text: str = Field(min_length=1, max_length=200)


class ResolveCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template: str
    note: str | None = Field(default=None, max_length=200)


class CloseCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=200)


class InternalNoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=200)


FeedbackStatus = Literal[
    "PENDING",
    "PROCESSING",
    "NEED_MORE",
    "USER_SUPPLIED",
    "RESOLVED",
    "CLOSED_INSUFFICIENT",
]
FeedbackCategory = Literal["CONTENT", "PRONUNCIATION", "DISPLAY", "FUNCTION"]
FeedbackSlaState = Literal["PAUSED", "OVERDUE", "DUE_SOON", "ON_TRACK", "COMPLETED"]


class FeedbackTicketResponse(BaseModel):
    id: str
    user_id: str
    category: FeedbackCategory
    description: str
    source: dict[str, Any]
    status: FeedbackStatus
    deadline_at: datetime | None
    sla_remaining_seconds: int | None
    supplement_rounds: int
    reopen_count: int
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    closed_at: datetime | None


class FeedbackListItemResponse(BaseModel):
    id: str
    user_id: str
    category: FeedbackCategory
    description: str
    status: FeedbackStatus
    deadline_at: datetime | None
    sla_state: FeedbackSlaState
    supplement_rounds: int
    created_at: datetime
    updated_at: datetime

    source: dict[str, Any] = Field(default_factory=dict)
    title: str = ""
    screenshot_status: str = "NONE"
    supplied_at: datetime | None = None


class FeedbackPageResponse(BaseModel):
    items: list[FeedbackListItemResponse]
    page: int
    page_size: int
    total: int


class FeedbackScreenshotResponse(BaseModel):
    security_status: str
    delete_after: datetime | None
    deleted_at: datetime | None


class FeedbackRoundResponse(BaseModel):
    round_number: int
    request_text: str | None
    supplement_text: str | None
    paused_at: datetime | None
    supplied_at: datetime | None


class FeedbackReplyResponse(BaseModel):
    template: str
    note: str | None
    admin_id: str
    sent_at: datetime


class FeedbackTimelineResponse(BaseModel):
    event_type: str
    actor_type: str
    actor_id: str
    visibility: str
    payload: dict[str, Any]
    occurred_at: datetime


class FeedbackInternalNoteResponse(BaseModel):
    id: str
    admin_id: str
    content: str
    created_at: datetime


class FeedbackAdminDetailResponse(FeedbackTicketResponse):
    screenshots: list[FeedbackScreenshotResponse]
    rounds: list[FeedbackRoundResponse]
    replies: list[FeedbackReplyResponse]
    timeline: list[FeedbackTimelineResponse]
    internal_notes: list[FeedbackInternalNoteResponse]


class SignedFeedbackScreenshotResponse(BaseModel):
    url: str
    expires_at: datetime


ServiceDependency = Callable[..., Awaitable[ServicePrincipal]]
AdminDependency = Callable[..., Awaitable[SessionRecord]]


def _serialize(ticket: FeedbackTicket) -> dict[str, object]:
    # 功能: 将反馈工单领域对象转换为接口响应字段.
    # 参数:
    #     ticket: 正在查询,持久化或变更的反馈工单.
    # 返回: 工单标识,所属用户,分类,说明,来源,状态,SLA,补充及重开次数和相关时间.
    return {
        "id": ticket.id,
        "user_id": ticket.user_id,
        "category": ticket.category,
        "description": ticket.description,
        "source": ticket.source,
        "status": ticket.status,
        "deadline_at": ticket.deadline_at,
        "sla_remaining_seconds": ticket.sla_remaining_seconds,
        "supplement_rounds": ticket.supplement_rounds,
        "reopen_count": ticket.reopen_count,
        "created_at": ticket.created_at,
        "updated_at": ticket.updated_at,
        "resolved_at": ticket.resolved_at,
        "closed_at": ticket.closed_at,
    }


def _serialize_user_detail(detail: FeedbackAdminDetail) -> dict[str, object]:
    # 功能: 将反馈详情转换为用户可见响应,过滤内部信息.
    # 参数:
    #     detail: 反馈详情对象,包含工单,时间线,回复及内部备注.
    # 返回: 工单字段及未删除截图对象键,最近回复,补充要求和用户补充记录.
    """Expose user-visible replies and supplied history without internal notes."""
    reply = detail.replies[-1] if detail.replies else None
    return {
        **_serialize(detail.ticket),
        "screenshots": [item.object_key for item in detail.screenshots if item.deleted_at is None],
        "reply": (reply.note or ("问题已处理" if reply.template == "RESOLVED" else "暂无法处理"))
        if reply
        else None,
        "reply_at": reply.sent_at if reply else None,
        "supplement_request": detail.rounds[-1].request_text if detail.rounds else None,
        "supplements": [
            {"text": item.supplement_text, "created_at": item.supplied_at}
            for item in detail.rounds
            if item.supplement_text is not None
        ],
    }


def _serialize_page(page: FeedbackAdminPage) -> dict[str, object]:
    # 功能: 将管理端反馈分页数据转换为接口响应.
    # 参数:
    #     page: 待序列化的反馈分页结果.
    # 返回: items 管理反馈列表及 page,page_size,total;列表项含 SLA 状态,截图状态及更新时间.
    return {
        "items": [
            {
                "id": item.id,
                "user_id": item.user_id,
                "category": item.category,
                "description": item.description,
                "title": item.description[:40],
                "status": item.status,
                "deadline_at": item.deadline_at,
                "sla_state": item.sla_state,
                "source": item.source,
                "screenshot_status": item.screenshot_status,
                "supplied_at": item.supplied_at,
                "supplement_rounds": item.supplement_rounds,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            }
            for item in page.items
        ],
        "page": page.page,
        "page_size": page.page_size,
        "total": page.total,
    }


def _serialize_admin_detail(detail: FeedbackAdminDetail) -> dict[str, object]:
    # 功能: 将管理端反馈详情及内部记录转换为接口响应.
    # 参数:
    #     detail: 反馈详情对象,包含工单,时间线,回复及内部备注.
    # 返回: 工单字段及截图安全状态,补充轮次,回复,时间线和内部备注列表.
    body = _serialize(detail.ticket)
    body.update(
        {
            "screenshots": [
                {
                    "security_status": item.security_status,
                    "delete_after": item.delete_after,
                    "deleted_at": item.deleted_at,
                }
                for item in detail.screenshots
            ],
            "rounds": [
                {
                    "round_number": item.round_number,
                    "request_text": item.request_text,
                    "supplement_text": item.supplement_text,
                    "paused_at": item.paused_at,
                    "supplied_at": item.supplied_at,
                }
                for item in detail.rounds
            ],
            "replies": [
                {
                    "template": item.template,
                    "note": item.note,
                    "admin_id": item.admin_id,
                    "sent_at": item.sent_at,
                }
                for item in detail.replies
            ],
            "timeline": [
                {
                    "event_type": item.event_type,
                    "actor_type": item.actor_type,
                    "actor_id": item.actor_id,
                    "visibility": item.visibility,
                    "payload": item.payload,
                    "occurred_at": item.occurred_at,
                }
                for item in detail.timeline
            ],
            "internal_notes": [
                {
                    "id": item.id,
                    "admin_id": item.admin_id,
                    "content": item.content,
                    "created_at": item.created_at,
                }
                for item in detail.internal_notes
            ],
        }
    )
    return body


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


async def _record_audit(
    audit_service: AuditService | None,
    *,
    actor_id: str,
    action: str,
    ticket_id: str,
    request_id: str,
    now: datetime,
    after_summary: dict[str, object],
) -> None:
    # 功能: 按配置记录反馈管理命令的审计摘要.
    # 参数:
    #     audit_service: 可选审计服务,未配置时跳过审计写入.
    #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
    #     action: 写入审计日志的操作名称.
    #     ticket_id: 反馈工单公开标识.
    #     request_id: 本次 HTTP 请求的关联标识,串联日志与审计.
    #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
    #     after_summary: 允许进入审计记录的操作后摘要.
    # 返回: 无返回值;正常完成表示本次操作成功.
    if audit_service is None:
        return
    await audit_service.record(
        AuditEvent(
            actor_public_id=actor_id,
            action=action,
            object_type="feedback",
            object_public_id=ticket_id,
            before_summary={},
            after_summary=after_summary,
            reason=None,
            request_id=request_id,
            occurred_at=now,
        )
    )


# 匿名函数: 为业务服务提供可注入的 UTC 当前时钟.
# 参数: 无.
# 返回: 当前带 UTC 时区的日期时间.
def create_internal_feedback_router(
    service: FeedbackService,
    *,
    current_service: ServiceDependency,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    # 功能: 创建小程序反馈查询,提交,补充和结果确认内部路由.
    # 参数:
    #     service: 执行业务操作的反馈工单服务.
    #     current_service: 校验内部服务请求签名并注入服务身份的依赖.
    #     clock: 返回当前带时区时间的回调,便于控制签名和业务时间.
    # 返回: 已注册业务端点的 FastAPI 路由对象.
    router = APIRouter(prefix="/internal/v1/feedback", tags=["internal-feedback"])

    @router.post("/query")
    async def query_feedback(
        payload: UserFeedbackQuery,
        _principal: Annotated[ServicePrincipal, Depends(current_service)],
    ) -> dict[str, object]:
        # 功能: 查询用户关联的反馈工单.
        # 参数:
        #     payload: 用户反馈查询条件.
        #     _principal: 内部签名校验后注入的调用服务身份.
        # 返回: items 用户反馈列表;每项含工单摘要,回复和 SLA 信息.
        items: list[dict[str, object]] = []
        page = 1
        while True:
            batch = await service.list_admin({"keyword": payload.user_id}, page, 100, clock())
            for item in batch.items:
                if item.user_id == payload.user_id:
                    items.append(_serialize_user_detail(await service.get_admin(item.id)))
            if page * 100 >= batch.total:
                break
            page += 1
        return {"items": items}

    @router.post("")
    async def create_feedback(
        payload: FeedbackCreateRequest,
        _principal: Annotated[ServicePrincipal, Depends(current_service)],
        idempotency_key: Annotated[str, Header(alias="X-Idempotency-Key")],
    ) -> dict[str, object]:
        # 功能: 创建用户反馈并保证请求幂等.
        # 参数:
        #     payload: 反馈用户,分类,说明,来源及截图对象键.
        #     _principal: 内部签名校验后注入的调用服务身份.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        # 返回: 新建或按幂等键重放的工单字段,含标识,状态,SLA 截止及创建时间.
        return _serialize(
            await service.create(
                payload.user_id,
                payload.category,
                payload.description,
                payload.source,
                payload.screenshots,
                idempotency_key,
                clock(),
            )
        )

    @router.get("/{ticket_id}")
    async def feedback_detail(
        ticket_id: str,
        _principal: Annotated[ServicePrincipal, Depends(current_service)],
    ) -> dict[str, object]:
        # 功能: 返回用户可见的反馈工单详情.
        # 参数:
        #     ticket_id: 反馈工单公开标识.
        #     _principal: 内部签名校验后注入的调用服务身份.
        # 返回: 用户可见工单详情,含截图对象键,最近回复,补充要求和用户补充记录.
        return _serialize_user_detail(await service.get_admin(ticket_id))

    @router.post("/{ticket_id}/supplements")
    async def supply(
        ticket_id: str,
        payload: SupplementRequest,
        _principal: Annotated[ServicePrincipal, Depends(current_service)],
        idempotency_key: Annotated[str, Header(alias="X-Idempotency-Key")],
    ) -> dict[str, object]:
        # 功能: 提交用户补充信息并重新启动反馈响应计时.
        # 参数:
        #     ticket_id: 反馈工单公开标识.
        #     payload: 反馈所属用户,补充说明及可选截图对象键.
        #     _principal: 内部签名校验后注入的调用服务身份.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        # 返回: 补充后的工单字段,状态为 USER_SUPPLIED 并包含重新计算的 SLA 截止时间.
        return _serialize(
            await service.supply(
                ticket_id,
                payload.text,
                payload.user_id,
                idempotency_key,
                clock(),
                screenshots=payload.screenshots,
            )
        )

    @router.post("/{ticket_id}/resolution")
    async def user_resolution(
        ticket_id: str,
        payload: UserResolutionRequest,
        _principal: Annotated[ServicePrincipal, Depends(current_service)],
        idempotency_key: Annotated[str, Header(alias="X-Idempotency-Key")],
    ) -> dict[str, object]:
        # 功能: 处理用户对反馈结果的确认或重开请求.
        # 参数:
        #     ticket_id: 反馈工单公开标识.
        #     payload: 反馈所属用户,结果确认命令及可选重开原因.
        #     _principal: 内部签名校验后注入的调用服务身份.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        # 返回: 重开后的工单字段或仅确认后读取的现有工单字段,包含状态,SLA 和重开次数.
        if payload.action != "REOPEN":
            return _serialize(await service.get(ticket_id))
        return _serialize(
            await service.reopen(
                ticket_id,
                payload.reason or "仍有问题",
                payload.user_id,
                idempotency_key,
                clock(),
            )
        )

    return router


# 匿名函数: 为业务服务提供可注入的 UTC 当前时钟.
# 参数: 无.
# 返回: 当前带 UTC 时区的日期时间.
def create_admin_feedback_router(
    service: FeedbackService,
    *,
    current_admin: AdminDependency,
    current_admin_write: AdminDependency | None = None,
    media_service: MediaService | None = None,
    audit_service: AuditService | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    # 功能: 创建反馈管理,截图查看和状态处理路由.
    # 参数:
    #     service: 执行业务操作的反馈工单服务.
    #     current_admin: 注入已认证管理员会话的只读依赖.
    #     current_admin_write: 同时校验管理员身份和 CSRF 的写操作依赖.
    #     media_service: 媒体对象查询,签名和安全校验服务.
    #     audit_service: 可选审计服务,未配置时跳过审计写入.
    #     clock: 返回当前带时区时间的回调,便于控制签名和业务时间.
    # 返回: 已注册业务端点的 FastAPI 路由对象.
    router = APIRouter(prefix="/api/v1/admin/feedback", tags=["feedback"])
    write_dependency = current_admin_write or current_admin

    @router.get("", response_model=FeedbackPageResponse)
    async def list_feedback(
        request: Request,
        response: Response,
        admin: Annotated[SessionRecord, Depends(current_admin)],
        status: Annotated[FeedbackStatus | None, Query()] = None,
        category: Annotated[FeedbackCategory | None, Query()] = None,
        keyword: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
        sla: Annotated[
            Literal["PAUSED", "OVERDUE", "DUE_SOON", "ON_TRACK", "COMPLETED", "URGENT"] | None,
            Query(),
        ] = None,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> dict[str, object]:
        # 功能: 分页查询管理端反馈及 SLA 状态.
        # 参数:
        #     request: 当前 HTTP 请求,提供头部,路径,请求体及请求关联上下文.
        #     response: 当前 HTTP 响应,用于设置 Cookie,禁止缓存等头部.
        #     admin: 经认证且按接口要求完成 CSRF 校验的管理员会话.
        #     status: 活动,反馈或权益的业务状态筛选条件.
        #     category: 反馈分类,例如 CONTENT,PRONUNCIATION,DISPLAY 或 FUNCTION.
        #     keyword: 反馈或用户公开标识的精确检索词,接口限制为 1 至 64 字符.
        #     sla: 反馈响应时限状态筛选条件,例如 OVERDUE 或 DUE_SOON.
        #     page: 分页页码,从 1 开始,默认第 1 页.
        #     page_size: 每页返回条数,接口范围为 1 至 100,默认 20.
        # 返回: items 管理反馈列表及 total,page,page_size,列表项包含 SLA 和截图状态.
        _no_store(response)
        filters = {
            key: value
            for key, value in {
                "status": status,
                "category": category,
                "keyword": keyword,
                "sla": sla,
            }.items()
            if value is not None
        }
        result = await service.list_admin(filters, page, page_size, clock())
        await _record_audit(
            audit_service,
            actor_id=str(admin.admin_user_id),
            action="feedback.list",
            ticket_id="list",
            request_id=_request_id(request),
            now=clock(),
            after_summary={
                "count": len(result.items),
                "status": status or "ALL",
                "category": category or "ALL",
                "sla": sla or "ALL",
            },
        )
        return _serialize_page(result)

    @router.get("/{ticket_id}", response_model=FeedbackAdminDetailResponse)
    async def detail(
        ticket_id: str,
        request: Request,
        response: Response,
        admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, object]:
        # 功能: 查询反馈工单详情并记录管理员访问审计.
        # 参数:
        #     ticket_id: 反馈工单公开标识.
        #     request: 当前 HTTP 请求,提供头部,路径,请求体及请求关联上下文.
        #     response: 当前 HTTP 响应,用于设置 Cookie,禁止缓存等头部.
        #     admin: 经认证且按接口要求完成 CSRF 校验的管理员会话.
        # 返回: 工单字段及管理员可见截图状态,补充轮次,回复,时间线和内部备注.
        _no_store(response)
        result = await service.get_admin(ticket_id)
        await _record_audit(
            audit_service,
            actor_id=str(admin.admin_user_id),
            action="feedback.view",
            ticket_id=ticket_id,
            request_id=_request_id(request),
            now=clock(),
            after_summary={"status": result.ticket.status},
        )
        return _serialize_admin_detail(result)

    @router.post(
        "/{ticket_id}/screenshot-url",
        response_model=SignedFeedbackScreenshotResponse,
    )
    async def screenshot_url(
        ticket_id: str,
        request: Request,
        response: Response,
        admin: Annotated[SessionRecord, Depends(write_dependency)],
    ) -> dict[str, object]:
        # 功能: 校验反馈截图关联并签发短期访问地址.
        # 参数:
        #     ticket_id: 反馈工单公开标识.
        #     request: 当前 HTTP 请求,提供头部,路径,请求体及请求关联上下文.
        #     response: 当前 HTTP 响应,用于设置 Cookie,禁止缓存等头部.
        #     admin: 经认证且按接口要求完成 CSRF 校验的管理员会话.
        # 返回: 短期截图签名访问地址 url 和失效时间 expires_at.
        _no_store(response)
        if media_service is None:
            raise AppError("FEEDBACK_SCREENSHOT_UNAVAILABLE", "反馈截图能力不可用", 503)
        detail = await service.get_admin(ticket_id)
        screenshot = next(
            (
                item
                for item in detail.screenshots
                if item.security_status == "PASSED" and item.deleted_at is None
            ),
            None,
        )
        if screenshot is None:
            raise AppError("FEEDBACK_SCREENSHOT_NOT_FOUND", "反馈截图不存在", 404)
        signed = await media_service.sign_feedback_screenshot(
            screenshot.object_key,
            screenshot.security_status,
            screenshot.deleted_at,
            clock(),
        )
        await _record_audit(
            audit_service,
            actor_id=str(admin.admin_user_id),
            action="feedback.screenshot.view",
            ticket_id=ticket_id,
            request_id=_request_id(request),
            now=clock(),
            after_summary={"security_status": screenshot.security_status},
        )
        return {"url": signed.url, "expires_at": signed.expires_at}

    @router.post(
        "/{ticket_id}/internal-notes",
        response_model=FeedbackInternalNoteResponse,
    )
    async def add_internal_note(
        ticket_id: str,
        payload: InternalNoteRequest,
        request: Request,
        response: Response,
        admin: Annotated[SessionRecord, Depends(write_dependency)],
        idempotency_key: Annotated[
            str,
            Header(alias="X-Idempotency-Key", min_length=1, max_length=128),
        ],
    ) -> dict[str, object]:
        # 功能: 保存仅管理员可见的反馈备注并保证幂等.
        # 参数:
        #     ticket_id: 反馈工单公开标识.
        #     payload: 仅管理员可见的反馈备注正文.
        #     request: 当前 HTTP 请求,提供头部,路径,请求体及请求关联上下文.
        #     response: 当前 HTTP 响应,用于设置 Cookie,禁止缓存等头部.
        #     admin: 经认证且按接口要求完成 CSRF 校验的管理员会话.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        # 返回: 保存后的内部备注 id,admin_id,content 和 created_at.
        _no_store(response)
        result = await service.add_internal_note(
            ticket_id,
            str(admin.admin_user_id),
            payload.content,
            idempotency_key,
            clock(),
        )
        await _record_audit(
            audit_service,
            actor_id=str(admin.admin_user_id),
            action="feedback.internal_note.add",
            ticket_id=ticket_id,
            request_id=_request_id(request),
            now=clock(),
            after_summary={"note_id": result.id},
        )
        return {
            "id": result.id,
            "admin_id": result.admin_id,
            "content": result.content,
            "created_at": result.created_at,
        }

    @router.post("/{ticket_id}/commands/start", response_model=FeedbackTicketResponse)
    async def start(
        ticket_id: str,
        request: Request,
        response: Response,
        admin: Annotated[SessionRecord, Depends(write_dependency)],
        idempotency_key: Annotated[
            str,
            Header(alias="X-Idempotency-Key", min_length=1, max_length=128),
        ],
    ) -> dict[str, object]:
        # 功能: 将允许处理的反馈状态切换为处理中.
        # 参数:
        #     ticket_id: 反馈工单公开标识.
        #     request: 当前 HTTP 请求,提供头部,路径,请求体及请求关联上下文.
        #     response: 当前 HTTP 响应,用于设置 Cookie,禁止缓存等头部.
        #     admin: 经认证且按接口要求完成 CSRF 校验的管理员会话.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        # 返回: 进入 PROCESSING 状态后的工单字段,含 SLA,补充及重开次数和相关时间.
        _no_store(response)
        result = await service.start_processing(
            ticket_id, str(admin.admin_user_id), idempotency_key, clock()
        )
        await _record_audit(
            audit_service,
            actor_id=str(admin.admin_user_id),
            action="feedback.command.start",
            ticket_id=ticket_id,
            request_id=_request_id(request),
            now=clock(),
            after_summary={"status": result.status},
        )
        return _serialize(result)

    @router.post(
        "/{ticket_id}/commands/request-supplement",
        response_model=FeedbackTicketResponse,
    )
    async def request_supplement(
        ticket_id: str,
        payload: SupplementCommand,
        request: Request,
        response: Response,
        admin: Annotated[SessionRecord, Depends(write_dependency)],
        idempotency_key: Annotated[
            str,
            Header(alias="X-Idempotency-Key", min_length=1, max_length=128),
        ],
    ) -> dict[str, object]:
        # 功能: 要求用户补充反馈信息并暂停 SLA 计时.
        # 参数:
        #     ticket_id: 反馈工单公开标识.
        #     payload: 管理员要求用户补充的说明.
        #     request: 当前 HTTP 请求,提供头部,路径,请求体及请求关联上下文.
        #     response: 当前 HTTP 响应,用于设置 Cookie,禁止缓存等头部.
        #     admin: 经认证且按接口要求完成 CSRF 校验的管理员会话.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        # 返回: 进入 NEED_MORE 状态的工单字段,含剩余 SLA 秒数和累计补充轮次.
        _no_store(response)
        result = await service.request_supplement(
            ticket_id,
            payload.request_text,
            str(admin.admin_user_id),
            idempotency_key,
            clock(),
        )
        await _record_audit(
            audit_service,
            actor_id=str(admin.admin_user_id),
            action="feedback.command.request_supplement",
            ticket_id=ticket_id,
            request_id=_request_id(request),
            now=clock(),
            after_summary={"status": result.status},
        )
        return _serialize(result)

    @router.post("/{ticket_id}/commands/resolve", response_model=FeedbackTicketResponse)
    async def resolve(
        ticket_id: str,
        payload: ResolveCommand,
        request: Request,
        response: Response,
        admin: Annotated[SessionRecord, Depends(write_dependency)],
        idempotency_key: Annotated[
            str,
            Header(alias="X-Idempotency-Key", min_length=1, max_length=128),
        ],
    ) -> dict[str, object]:
        # 功能: 通过处理模板解决反馈并生成用户通知.
        # 参数:
        #     ticket_id: 反馈工单公开标识.
        #     payload: 反馈解决回复模板及可选说明.
        #     request: 当前 HTTP 请求,提供头部,路径,请求体及请求关联上下文.
        #     response: 当前 HTTP 响应,用于设置 Cookie,禁止缓存等头部.
        #     admin: 经认证且按接口要求完成 CSRF 校验的管理员会话.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        # 返回: 进入 RESOLVED 状态的工单字段,包含 resolved_at,且清除 SLA 截止时间.
        _no_store(response)
        result = await service.resolve(
            ticket_id,
            payload.template,
            payload.note,
            str(admin.admin_user_id),
            idempotency_key,
            clock(),
        )
        await _record_audit(
            audit_service,
            actor_id=str(admin.admin_user_id),
            action="feedback.command.resolve",
            ticket_id=ticket_id,
            request_id=_request_id(request),
            now=clock(),
            after_summary={"status": result.status, "template": payload.template},
        )
        return _serialize(result)

    @router.post(
        "/{ticket_id}/commands/close-insufficient",
        response_model=FeedbackTicketResponse,
    )
    async def close_insufficient(
        ticket_id: str,
        payload: CloseCommand,
        request: Request,
        response: Response,
        admin: Annotated[SessionRecord, Depends(write_dependency)],
        idempotency_key: Annotated[
            str,
            Header(alias="X-Idempotency-Key", min_length=1, max_length=128),
        ],
    ) -> dict[str, object]:
        # 功能: 以信息不足原因关闭反馈并设置截图保留期.
        # 参数:
        #     ticket_id: 反馈工单公开标识.
        #     payload: 信息不足关闭原因.
        #     request: 当前 HTTP 请求,提供头部,路径,请求体及请求关联上下文.
        #     response: 当前 HTTP 响应,用于设置 Cookie,禁止缓存等头部.
        #     admin: 经认证且按接口要求完成 CSRF 校验的管理员会话.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        # 返回: 进入 CLOSED_INSUFFICIENT 状态的工单字段,含 closed_at 且清除 SLA 截止时间.
        _no_store(response)
        result = await service.close_insufficient(
            ticket_id,
            payload.reason,
            str(admin.admin_user_id),
            idempotency_key,
            clock(),
        )
        await _record_audit(
            audit_service,
            actor_id=str(admin.admin_user_id),
            action="feedback.command.close_insufficient",
            ticket_id=ticket_id,
            request_id=_request_id(request),
            now=clock(),
            after_summary={"status": result.status},
        )
        return _serialize(result)

    return router
