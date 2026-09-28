from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.media.service import MediaService


class UploadPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_type: str


class ConfirmUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_type: str
    object_key: str = Field(min_length=1, max_length=512)


AdminDependency = Callable[..., Awaitable[SessionRecord]]
SignedTargetResolver = Callable[[str, str, datetime], Awaitable[tuple[str, datetime | None]]]


def create_media_router(
    service: MediaService,
    *,
    current_admin_write: AdminDependency,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/media", tags=["admin-media"])

    @router.post("/upload-policies")
    async def create_upload_policy(
        payload: UploadPolicyRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, object]:
        policy = await service.create_upload_policy(payload.asset_type, str(admin.admin_user_id))
        return {
            "upload_url": policy.upload_url,
            "object_key_prefix": policy.object_key_prefix,
            "max_bytes": policy.max_bytes,
            "expires_in": policy.expires_in,
            "fields": policy.fields,
        }

    @router.post("/uploads/confirm", status_code=201)
    async def confirm_upload(
        payload: ConfirmUploadRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, object]:
        asset = await service.confirm_upload(
            payload.asset_type, str(admin.admin_user_id), payload.object_key, clock()
        )
        return {
            "id": asset.id,
            "asset_type": asset.asset_type,
            "content_type": asset.content_type,
            "size": asset.size,
            "sha256": asset.sha256,
            "status": asset.status,
        }

    return router


def create_internal_media_router(
    service: MediaService,
    *,
    resolve_target: SignedTargetResolver,
    current_service: Callable[..., Awaitable[object]],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter(prefix="/internal/v1/media", tags=["internal-media"])

    @router.get("/{target_id}/signed-url")
    async def signed_url(
        target_id: str,
        user_id: Annotated[str, Header(alias="X-User-ID")],
        _principal: Annotated[object, Depends(current_service)],
    ) -> dict[str, object]:
        now = clock()
        object_key, entitlement_expires_at = await resolve_target(target_id, user_id, now)
        signed = await service.sign_media(object_key, entitlement_expires_at, now)
        return {"url": signed.url, "expires_at": signed.expires_at}

    return router
