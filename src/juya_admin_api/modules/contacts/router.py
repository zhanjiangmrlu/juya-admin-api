from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from juya_admin_api.integrations.miniapp_api.client import (
    ContactCorrection,
    ContactProjection,
    CorrectionDecision,
)
from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.contacts.service import ContactAdminService

AdminDependency = Callable[..., Awaitable[SessionRecord]]
ContactStatus = Literal[
    "NOT_PROVIDED",
    "PENDING",
    "CONTACTED",
    "UNREACHABLE",
    "DO_NOT_CONTACT",
]
CorrectionStatus = Literal["PENDING", "PROCESSING", "APPROVED", "REJECTED", "CANCELLED"]
CorrectionCommand = Literal["approve", "reject"]


class ContactStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ContactStatus


class ContactProjectionResponse(BaseModel):
    user_id: str
    wechat_id: str | None
    contact_status: ContactStatus
    change_pending: bool
    verified_at: datetime | None
    verified_by: str | None
    updated_at: datetime


class ContactTimelineResponse(BaseModel):
    status: str
    actor_type: str
    actor_id: str
    event_type: str
    occurred_at: datetime


class ContactCorrectionResponse(BaseModel):
    id: str
    user_id: str
    juya_number: str
    nickname: str | None
    wechat_id: str | None
    reason: str
    status: CorrectionStatus
    created_at: datetime
    processed_at: datetime | None
    timeline: list[ContactTimelineResponse]


class ContactCorrectionPageResponse(BaseModel):
    items: list[ContactCorrectionResponse]
    total: int
    page: int
    page_size: int


class CorrectionDecisionResponse(BaseModel):
    id: str
    status: Literal["APPROVED", "REJECTED"]
    processed_at: datetime


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


def _contact_body(contact: ContactProjection) -> dict[str, object]:
    # 功能: 把联系信息投影转换为接口响应.
    # 参数:
    #     contact: 从小程序服务获得的联系人信息投影.
    # 返回: 联系人投影的展示字段,包括微信号,跟进状态和变更时间.
    return {
        "user_id": contact.user_id,
        "wechat_id": contact.wechat_id,
        "contact_status": contact.contact_status,
        "change_pending": contact.change_pending,
        "verified_at": contact.verified_at,
        "verified_by": contact.verified_by,
        "updated_at": contact.updated_at,
    }


def _correction_body(correction: ContactCorrection) -> dict[str, object]:
    # 功能: 把联系信息纠错申请转换为接口响应.
    # 参数:
    #     correction: 联系人纠错申请的业务记录.
    # 返回: 纠错申请标识,用户,说明,处理状态和时间字段.
    return {
        "id": correction.id,
        "user_id": correction.user_id,
        "juya_number": correction.juya_number,
        "nickname": correction.nickname,
        "wechat_id": correction.wechat_id,
        "reason": correction.reason,
        "status": correction.status,
        "created_at": correction.created_at,
        "processed_at": correction.processed_at,
        "timeline": [
            {
                "status": item.status,
                "actor_type": item.actor_type,
                "actor_id": item.actor_id,
                "event_type": item.event_type,
                "occurred_at": item.occurred_at,
            }
            for item in correction.timeline
        ],
    }


def _decision_body(decision: CorrectionDecision) -> dict[str, object]:
    # 功能: 把纠错处理结果转换为接口响应.
    # 参数:
    #     decision: 纠错申请处理结果对象,包含申请标识,状态和处理时间.
    # 返回: 纠错申请处理状态及决定结果字段.
    return {
        "id": decision.id,
        "status": decision.status,
        "processed_at": decision.processed_at,
    }


# 匿名函数: 为业务服务提供可注入的 UTC 当前时钟.
# 参数: 无.
# 返回: 当前带 UTC 时区的日期时间.
def create_contact_router(
    service: ContactAdminService,
    *,
    current_admin: AdminDependency,
    current_admin_write: AdminDependency,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    # 功能: 创建联系信息查询,跟进,核实及纠错管理路由.
    # 参数:
    #     service: 执行业务操作的用户联系信息服务.
    #     current_admin: 注入已认证管理员会话的只读依赖.
    #     current_admin_write: 同时校验管理员身份和 CSRF 的写操作依赖.
    #     clock: 返回当前带时区时间的回调,便于控制签名和业务时间.
    # 返回: 已注册业务端点的 FastAPI 路由对象.
    router = APIRouter(prefix="/api/v1/admin", tags=["admin-contacts"])

    @router.get("/contact-corrections", response_model=ContactCorrectionPageResponse)
    async def list_corrections(
        request: Request,
        response: Response,
        admin: Annotated[SessionRecord, Depends(current_admin)],
        status: Annotated[CorrectionStatus | None, Query()] = None,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> dict[str, object]:
        # 功能: 分页读取联系信息纠错申请并记录访问审计.
        # 参数:
        #     request: 当前 HTTP 请求,提供头部,路径,请求体及请求关联上下文.
        #     response: 当前 HTTP 响应,用于设置 Cookie,禁止缓存等头部.
        #     admin: 经认证且按接口要求完成 CSRF 校验的管理员会话.
        #     status: 联系人跟进状态或纠错处理状态.
        #     page: 分页页码,从 1 开始,默认第 1 页.
        #     page_size: 每页返回条数,接口范围为 1 至 100,默认 20.
        # 返回: items 纠错申请列表及 total,page,page_size 分页字段.
        _no_store(response)
        result = await service.list_corrections(
            status,
            page,
            page_size,
            str(admin.admin_user_id),
            _request_id(request),
            clock(),
        )
        return {
            "items": [_correction_body(item) for item in result.items],
            "total": result.total,
            "page": result.page,
            "page_size": result.page_size,
        }

    @router.get(
        "/contact-corrections/{correction_id}",
        response_model=ContactCorrectionResponse,
    )
    async def correction_detail(
        correction_id: str,
        request: Request,
        response: Response,
        admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, object]:
        # 功能: 读取联系信息纠错申请详情并记录访问审计.
        # 参数:
        #     correction_id: 联系人纠错申请公开标识.
        #     request: 当前 HTTP 请求,提供头部,路径,请求体及请求关联上下文.
        #     response: 当前 HTTP 响应,用于设置 Cookie,禁止缓存等头部.
        #     admin: 经认证且按接口要求完成 CSRF 校验的管理员会话.
        # 返回: 纠错申请标识,用户资料,微信号,原因,状态,处理时间和时间线.
        _no_store(response)
        result = await service.get_correction(
            correction_id,
            str(admin.admin_user_id),
            _request_id(request),
            clock(),
        )
        return _correction_body(result)

    @router.post(
        "/contact-corrections/{correction_id}/commands/{command}",
        response_model=CorrectionDecisionResponse,
    )
    async def decide_correction(
        correction_id: str,
        command: CorrectionCommand,
        request: Request,
        response: Response,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: Annotated[
            str,
            Header(alias="X-Idempotency-Key", min_length=1, max_length=128),
        ],
    ) -> dict[str, object]:
        # 功能: 执行联系信息纠错决定并记录审计.
        # 参数:
        #     correction_id: 联系人纠错申请公开标识.
        #     command: 管理员对联系信息纠错申请作出的决定.
        #     request: 当前 HTTP 请求,提供头部,路径,请求体及请求关联上下文.
        #     response: 当前 HTTP 响应,用于设置 Cookie,禁止缓存等头部.
        #     admin: 经认证且按接口要求完成 CSRF 校验的管理员会话.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        # 返回: 纠错申请 id,处理后的 status 和 processed_at 时间.
        _no_store(response)
        result = await service.decide_correction(
            correction_id,
            "APPROVED" if command == "approve" else "REJECTED",
            str(admin.admin_user_id),
            idempotency_key,
            _request_id(request),
            clock(),
        )
        return _decision_body(result)

    @router.post(
        "/users/{user_id}/commands/contact-status",
        response_model=ContactProjectionResponse,
    )
    async def update_contact_status(
        user_id: str,
        payload: ContactStatusRequest,
        request: Request,
        response: Response,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, object]:
        # 功能: 更新用户联系信息的跟进状态.
        # 参数:
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     payload: 需要更新的联系人跟进状态.
        #     request: 当前 HTTP 请求,提供头部,路径,请求体及请求关联上下文.
        #     response: 当前 HTTP 响应,用于设置 Cookie,禁止缓存等头部.
        #     admin: 经认证且按接口要求完成 CSRF 校验的管理员会话.
        # 返回: 更新后的用户联系投影,含微信号,跟进状态,待核实变更标记和相关时间.
        _no_store(response)
        result = await service.update_status(
            user_id,
            payload.status,
            str(admin.admin_user_id),
            _request_id(request),
            clock(),
        )
        return _contact_body(result)

    @router.post(
        "/users/{user_id}/commands/verify-contact-change",
        response_model=ContactProjectionResponse,
    )
    async def verify_contact_change(
        user_id: str,
        request: Request,
        response: Response,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, object]:
        # 功能: 确认用户联系信息变更已经核实.
        # 参数:
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     request: 当前 HTTP 请求,提供头部,路径,请求体及请求关联上下文.
        #     response: 当前 HTTP 响应,用于设置 Cookie,禁止缓存等头部.
        #     admin: 经认证且按接口要求完成 CSRF 校验的管理员会话.
        # 返回: 核实后的联系投影,含微信号,跟进状态,核实时间和核实管理员标识.
        _no_store(response)
        result = await service.verify_change(
            user_id,
            str(admin.admin_user_id),
            _request_id(request),
            clock(),
        )
        return _contact_body(result)

    @router.post("/users/{user_id}/contact-copy-events", status_code=204)
    async def audit_contact_copy(
        user_id: str,
        request: Request,
        response: Response,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> None:
        # 功能: 记录管理员复制用户联系信息的敏感操作.
        # 参数:
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     request: 当前 HTTP 请求,提供头部,路径,请求体及请求关联上下文.
        #     response: 当前 HTTP 响应,用于设置 Cookie,禁止缓存等头部.
        #     admin: 经认证且按接口要求完成 CSRF 校验的管理员会话.
        # 返回: 无返回值;正常完成表示本次操作成功.
        _no_store(response)
        await service.audit_copy(
            user_id,
            str(admin.admin_user_id),
            _request_id(request),
            clock(),
        )

    return router
