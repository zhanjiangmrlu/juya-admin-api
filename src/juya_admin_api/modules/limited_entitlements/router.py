from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.limited_entitlements.domain import (
    LimitedEntitlement,
    RemedyMode,
)
from juya_admin_api.modules.limited_entitlements.service import LimitedEntitlementService


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
    current_admin_write: AdminDependency,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/limited-entitlements",
        tags=["limited-entitlements"],
    )

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
    ) -> dict[str, object]:
        return _serialize(
            await service.pause(
                entitlement_id,
                str(admin.admin_user_id),
                payload.reason,
                clock(),
            )
        )

    @router.post("/{entitlement_id}/commands/resume")
    async def resume(
        entitlement_id: str,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, object]:
        return _serialize(await service.resume(entitlement_id, str(admin.admin_user_id), clock()))

    @router.post("/{entitlement_id}/commands/revoke")
    async def revoke(
        entitlement_id: str,
        payload: ReasonRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, object]:
        return _serialize(
            await service.revoke(
                entitlement_id,
                str(admin.admin_user_id),
                payload.reason,
                clock(),
            )
        )

    return router
