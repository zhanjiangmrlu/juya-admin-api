from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.content.service import ContentService


class CreateRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_revision_id: str | None = None


class PublishRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    acknowledged_warning_codes: set[str] = Field(default_factory=set)


class OpenScenesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene_ids: list[str] = Field(min_length=3, max_length=3)


class PreviewScenesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene_ids: list[str] = Field(min_length=3, max_length=6)


AdminDependency = Callable[..., Awaitable[SessionRecord]]


def create_content_router(
    service: ContentService,
    *,
    current_admin: AdminDependency,
    current_admin_write: AdminDependency,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/content", tags=["admin-content"])

    @router.post("/scenes/{scene_id}/revisions", status_code=201)
    async def create_revision(
        scene_id: str,
        payload: CreateRevisionRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, object]:
        revision = await service.create_revision(
            scene_id,
            payload.source_revision_id,
            str(admin.admin_user_id),
            clock(),
        )
        return {
            "id": revision.id,
            "scene_id": revision.scene_id,
            "source_revision_id": revision.source_revision_id,
            "status": revision.status,
        }

    @router.post("/revisions/{revision_id}/publish-checks")
    async def validate_publish(
        revision_id: str,
        payload: PublishRevisionRequest,
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, object]:
        summary = await service.validate_publish(
            revision_id, frozenset(payload.acknowledged_warning_codes)
        )
        return {
            "revision_id": summary.revision_id,
            "ready": summary.ready,
            "error_codes": summary.error_codes,
            "warning_codes": summary.warning_codes,
        }

    @router.post("/revisions/{revision_id}/commands/publish")
    async def publish_revision(
        revision_id: str,
        payload: PublishRevisionRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: Annotated[str, Header(alias="X-Idempotency-Key")],
    ) -> dict[str, object]:
        published = await service.publish_revision(
            revision_id,
            str(admin.admin_user_id),
            idempotency_key,
            clock(),
            acknowledged_warning_codes=frozenset(payload.acknowledged_warning_codes),
        )
        return {
            "scene_id": published.scene_id,
            "revision_id": published.revision_id,
            "published_at": published.published_at,
        }

    @router.put("/open-scenes")
    async def replace_open_scenes(
        payload: OpenScenesRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, object]:
        scene_ids = cast(tuple[str, str, str], tuple(payload.scene_ids))
        config = await service.replace_open_scenes(scene_ids, str(admin.admin_user_id), clock())
        return {
            "version": config.version,
            "scene_ids": config.scene_ids,
            "activated_at": config.activated_at,
        }

    @router.put("/preview-configs/{series_id}")
    async def replace_preview_scenes(
        series_id: str,
        payload: PreviewScenesRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, object]:
        config = await service.replace_preview_scenes(
            series_id,
            tuple(payload.scene_ids),
            str(admin.admin_user_id),
            clock(),
        )
        return {
            "series_id": config.series_id,
            "scene_ids": config.scene_ids,
            "updated_at": config.updated_at,
        }

    @router.post("/scenes/{scene_id}/commands/offline", status_code=204)
    async def offline_scene(
        scene_id: str,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> None:
        await service.offline_scene(scene_id, str(admin.admin_user_id), clock())

    return router
