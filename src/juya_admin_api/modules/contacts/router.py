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
    return str(getattr(request.state, "request_id", "unknown"))


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _contact_body(contact: ContactProjection) -> dict[str, object]:
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
    return {
        "id": decision.id,
        "status": decision.status,
        "processed_at": decision.processed_at,
    }


def create_contact_router(
    service: ContactAdminService,
    *,
    current_admin: AdminDependency,
    current_admin_write: AdminDependency,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
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
        _no_store(response)
        await service.audit_copy(
            user_id,
            str(admin.admin_user_id),
            _request_id(request),
            clock(),
        )

    return router
