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
    return str(getattr(request.state, "request_id", "unknown"))


def _no_store(response: Response) -> None:
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


def create_internal_feedback_router(
    service: FeedbackService,
    *,
    current_service: ServiceDependency,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter(prefix="/internal/v1/feedback", tags=["internal-feedback"])

    @router.post("/query")
    async def query_feedback(
        payload: UserFeedbackQuery,
        _principal: Annotated[ServicePrincipal, Depends(current_service)],
    ) -> dict[str, object]:
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
        return _serialize_user_detail(await service.get_admin(ticket_id))

    @router.post("/{ticket_id}/supplements")
    async def supply(
        ticket_id: str,
        payload: SupplementRequest,
        _principal: Annotated[ServicePrincipal, Depends(current_service)],
        idempotency_key: Annotated[str, Header(alias="X-Idempotency-Key")],
    ) -> dict[str, object]:
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


def create_admin_feedback_router(
    service: FeedbackService,
    *,
    current_admin: AdminDependency,
    current_admin_write: AdminDependency | None = None,
    media_service: MediaService | None = None,
    audit_service: AuditService | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
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
