from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.formal_entitlements.router import EntitlementQueryRepository
from juya_admin_api.modules.limited_entitlements.domain import (
    LimitedEntitlement,
    RemedyMode,
)
from juya_admin_api.modules.limited_entitlements.service import LimitedEntitlementService
from juya_admin_api.shared.errors import AppError


class GrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=64)
    campaign_version_id: str = Field(min_length=1, max_length=64)


class RemedyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: RemedyMode


class ReasonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


AdminDependency = Callable[..., Awaitable[SessionRecord]]


class LimitedEntitlementDetailResponse(BaseModel):
    id: str
    user_id: str
    campaign_version_id: str
    campaign_id: str
    campaign_name: str
    status: str
    granted_at: datetime
    start_deadline: datetime
    activated_at: datetime | None
    expires_at: datetime | None
    remedy_count: int
    version: int
    duration_days: int
    activation_window_days: int
    scene_ids: list[str]
    available_operations: list[str]


def _serialize(entitlement: LimitedEntitlement) -> dict[str, object]:
    return {
        "id": entitlement.id,
        "user_id": entitlement.user_id,
        "campaign_version_id": entitlement.campaign_version_id,
        "status": entitlement.status,
        "granted_at": entitlement.granted_at,
        "start_deadline": entitlement.start_deadline,
        "activated_at": entitlement.activated_at,
        "expires_at": entitlement.expires_at,
        "remedy_count": entitlement.remedy_count,
        "version": entitlement.version,
    }


def create_limited_entitlement_router(
    service: LimitedEntitlementService,
    *,
    query_repository: EntitlementQueryRepository,
    current_admin: AdminDependency,
    current_admin_write: AdminDependency,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/limited-entitlements",
        tags=["limited-entitlements"],
    )

    @router.get("/{entitlement_id}", response_model=LimitedEntitlementDetailResponse)
    async def get_entitlement(
        entitlement_id: str,
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, Any]:
        result = await query_repository.get_limited(entitlement_id)
        if result is None:
            raise AppError("LIMITED_ENTITLEMENT_NOT_FOUND", "限时权益不存在", 404)
        return result

    @router.post("/commands/grant", status_code=201)
    async def grant(
        payload: GrantRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: Annotated[str, Header(alias="X-Idempotency-Key")],
    ) -> dict[str, object]:
        result = await service.grant(
            payload.user_id,
            payload.campaign_version_id,
            str(admin.admin_user_id),
            idempotency_key,
            clock(),
        )
        return _serialize(result)

    @router.post("/{entitlement_id}/commands/remedy")
    async def remedy(
        entitlement_id: str,
        payload: RemedyRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: Annotated[str, Header(alias="X-Idempotency-Key")],
    ) -> dict[str, object]:
        result = await service.remedy(
            entitlement_id,
            payload.mode,
            str(admin.admin_user_id),
            idempotency_key,
            clock(),
        )
        return _serialize(result)

    @router.post("/{entitlement_id}/commands/pause")
    async def pause(
        entitlement_id: str,
        payload: ReasonRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: Annotated[str, Header(alias="X-Idempotency-Key")],
    ) -> dict[str, object]:
        return _serialize(
            await service.pause(
                entitlement_id,
                str(admin.admin_user_id),
                idempotency_key,
                payload.reason,
                clock(),
            )
        )

    @router.post("/{entitlement_id}/commands/resume")
    async def resume(
        entitlement_id: str,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: Annotated[str, Header(alias="X-Idempotency-Key")],
    ) -> dict[str, object]:
        return _serialize(
            await service.resume(entitlement_id, str(admin.admin_user_id), idempotency_key, clock())
        )

    @router.post("/{entitlement_id}/commands/revoke")
    async def revoke(
        entitlement_id: str,
        payload: ReasonRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: Annotated[str, Header(alias="X-Idempotency-Key")],
    ) -> dict[str, object]:
        return _serialize(
            await service.revoke(
                entitlement_id,
                str(admin.admin_user_id),
                idempotency_key,
                payload.reason,
                clock(),
            )
        )

    return router
