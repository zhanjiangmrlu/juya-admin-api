from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.infrastructure.security.service_hmac import ServicePrincipal
from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.feedback.domain import FeedbackTicket
from juya_admin_api.modules.feedback.service import FeedbackService


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


def create_internal_feedback_router(
    service: FeedbackService,
    *,
    current_service: ServiceDependency,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter(prefix="/internal/v1/feedback", tags=["internal-feedback"])

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
        return _serialize(await service.get(ticket_id))

    @router.post("/{ticket_id}/supplements")
    async def supply(
        ticket_id: str,
        payload: SupplementRequest,
        _principal: Annotated[ServicePrincipal, Depends(current_service)],
        idempotency_key: Annotated[str, Header(alias="X-Idempotency-Key")],
    ) -> dict[str, object]:
        return _serialize(
            await service.supply(ticket_id, payload.text, payload.user_id, idempotency_key, clock())
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
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/feedback", tags=["feedback"])
    write_dependency = current_admin_write or current_admin

    @router.get("/{ticket_id}")
    async def detail(
        ticket_id: str,
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, object]:
        return _serialize(await service.get(ticket_id))

    @router.post("/{ticket_id}/commands/start")
    async def start(
        ticket_id: str,
        admin: Annotated[SessionRecord, Depends(write_dependency)],
        idempotency_key: Annotated[str, Header(alias="X-Idempotency-Key")],
    ) -> dict[str, object]:
        return _serialize(
            await service.start_processing(
                ticket_id, str(admin.admin_user_id), idempotency_key, clock()
            )
        )

    @router.post("/{ticket_id}/commands/request-supplement")
    async def request_supplement(
        ticket_id: str,
        payload: SupplementCommand,
        admin: Annotated[SessionRecord, Depends(write_dependency)],
        idempotency_key: Annotated[str, Header(alias="X-Idempotency-Key")],
    ) -> dict[str, object]:
        return _serialize(
            await service.request_supplement(
                ticket_id,
                payload.request_text,
                str(admin.admin_user_id),
                idempotency_key,
                clock(),
            )
        )

    @router.post("/{ticket_id}/commands/resolve")
    async def resolve(
        ticket_id: str,
        payload: ResolveCommand,
        admin: Annotated[SessionRecord, Depends(write_dependency)],
        idempotency_key: Annotated[str, Header(alias="X-Idempotency-Key")],
    ) -> dict[str, object]:
        return _serialize(
            await service.resolve(
                ticket_id,
                payload.template,
                payload.note,
                str(admin.admin_user_id),
                idempotency_key,
                clock(),
            )
        )

    @router.post("/{ticket_id}/commands/close-insufficient")
    async def close_insufficient(
        ticket_id: str,
        payload: CloseCommand,
        admin: Annotated[SessionRecord, Depends(write_dependency)],
        idempotency_key: Annotated[str, Header(alias="X-Idempotency-Key")],
    ) -> dict[str, object]:
        return _serialize(
            await service.close_insufficient(
                ticket_id,
                payload.reason,
                str(admin.admin_user_id),
                idempotency_key,
                clock(),
            )
        )

    return router
