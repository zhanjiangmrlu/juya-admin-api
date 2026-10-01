from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.infrastructure.observability.request_id import get_request_id
from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.audit.service import AuditEvent, AuditService
from juya_admin_api.modules.content.domain import (
    AdminPreview,
    DiscoveryConfig,
    Scene,
    ScenePage,
    SceneRevision,
)
from juya_admin_api.modules.content.schemas import SceneContent
from juya_admin_api.modules.content.service import ContentService


class CreateRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_revision_id: str | None = None


class PublishCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    acknowledged_warning_codes: set[str] = Field(default_factory=set)


class PublishRevisionRequest(PublishCheckRequest):
    expected_version: int = Field(ge=1)


class OpenScenesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene_ids: list[str] = Field(min_length=3, max_length=3)


class PreviewScenesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene_ids: list[str] = Field(min_length=3, max_length=6)


class SaveRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    content: SceneContent


class SaveDiscoveryConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    open_scene_ids: list[str] = Field(min_length=3, max_length=3)
    preview_by_series: dict[str, list[str]]
    learning_modules: dict[str, bool]


class SceneResponse(BaseModel):
    id: str
    series_id: str
    title: str
    series_title: str
    summary: str | None
    cover_object_key: str | None
    status: str
    draft_revision_id: str | None
    published_revision_id: str | None
    updated_at: datetime | None


class ScenePageResponse(BaseModel):
    items: list[SceneResponse]
    page: int
    page_size: int
    total: int


class RevisionResponse(BaseModel):
    id: str
    scene_id: str
    source_revision_id: str | None
    version: int
    status: str
    stable_sentence_ids: list[str]
    stable_entry_ids: list[str]
    content: SceneContent
    created_by: str
    created_at: datetime | None


class DiscoveryConfigResponse(BaseModel):
    version: int
    open_scene_ids: list[str]
    preview_by_series: dict[str, list[str]]
    learning_modules: dict[str, bool]
    updated_at: datetime | None
    actor_id: str | None


class AdminPreviewResponse(BaseModel):
    scene_id: str
    revision_id: str
    revision_status: str
    scene_title: str
    series_title: str
    content: SceneContent


AdminDependency = Callable[..., Awaitable[SessionRecord]]


def _serialize_scene(scene: Scene) -> dict[str, object]:
    return {
        "id": scene.id,
        "series_id": scene.series_id,
        "title": scene.title,
        "series_title": scene.series_title,
        "summary": scene.summary,
        "cover_object_key": scene.cover_object_key,
        "status": scene.status,
        "draft_revision_id": scene.draft_revision_id,
        "published_revision_id": scene.published_revision_id,
        "updated_at": scene.updated_at,
    }


def _serialize_page(page: ScenePage) -> dict[str, object]:
    return {
        "items": [_serialize_scene(item) for item in page.items],
        "page": page.page,
        "page_size": page.page_size,
        "total": page.total,
    }


def _serialize_revision(revision: SceneRevision) -> dict[str, object]:
    return {
        "id": revision.id,
        "scene_id": revision.scene_id,
        "source_revision_id": revision.source_revision_id,
        "version": revision.version,
        "status": revision.status,
        "stable_sentence_ids": list(revision.stable_sentence_ids),
        "stable_entry_ids": list(revision.stable_entry_ids),
        "content": revision.content,
        "created_by": revision.created_by,
        "created_at": revision.created_at,
    }


def _serialize_config(config: DiscoveryConfig) -> dict[str, object]:
    return {
        "version": config.version,
        "open_scene_ids": list(config.open_scene_ids),
        "preview_by_series": {
            series_id: list(scene_ids) for series_id, scene_ids in config.preview_by_series.items()
        },
        "learning_modules": config.learning_modules,
        "updated_at": config.updated_at,
        "actor_id": config.actor_id,
    }


def _serialize_preview(preview: AdminPreview) -> dict[str, object]:
    return {
        "scene_id": preview.scene_id,
        "revision_id": preview.revision_id,
        "revision_status": preview.revision_status,
        "scene_title": preview.scene_title,
        "series_title": preview.series_title,
        "content": preview.content,
    }


def create_content_router(
    service: ContentService,
    *,
    audit_service: AuditService,
    current_admin: AdminDependency,
    current_admin_write: AdminDependency,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/content", tags=["admin-content"])

    @router.get("/scenes", response_model=ScenePageResponse)
    async def list_scenes(
        _admin: Annotated[SessionRecord, Depends(current_admin)],
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
        query: Annotated[str | None, Query(max_length=200)] = None,
        series_id: str | None = None,
        status: Literal["DRAFT", "PUBLISHED", "OFFLINE"] | None = None,
    ) -> dict[str, object]:
        result = await service.list_scenes(
            page=page,
            page_size=page_size,
            query=query,
            series_id=series_id,
            status=status,
        )
        return _serialize_page(result)

    @router.get("/scenes/{scene_id}", response_model=SceneResponse)
    async def get_scene(
        scene_id: str,
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, object]:
        return _serialize_scene(await service.get_scene(scene_id))

    @router.get("/revisions/{revision_id}", response_model=RevisionResponse)
    async def get_revision(
        revision_id: str,
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, object]:
        return _serialize_revision(await service.get_revision(revision_id))

    @router.put("/revisions/{revision_id}", response_model=RevisionResponse)
    async def save_revision(
        revision_id: str,
        payload: SaveRevisionRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, object]:
        previous = await service.get_revision(revision_id)
        saved = await service.save_revision(
            revision_id,
            payload.content.model_dump(mode="json"),
            expected_version=payload.expected_version,
            actor_id=str(admin.admin_user_id),
        )
        await audit_service.record(
            AuditEvent(
                actor_public_id=str(admin.admin_user_id),
                action="content.revision.save",
                object_type="scene_revision",
                object_public_id=revision_id,
                before_summary={"version": previous.version, "status": previous.status},
                after_summary={"version": saved.version, "status": saved.status},
                reason=None,
                request_id=get_request_id(request),
                occurred_at=clock(),
            )
        )
        return _serialize_revision(saved)

    @router.get("/discovery-config", response_model=DiscoveryConfigResponse)
    async def get_discovery_config(
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, object]:
        return _serialize_config(await service.get_discovery_config())

    @router.put("/discovery-config", response_model=DiscoveryConfigResponse)
    async def save_discovery_config(
        payload: SaveDiscoveryConfigRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, object]:
        open_scene_ids = cast(tuple[str, str, str], tuple(payload.open_scene_ids))
        saved = await service.save_discovery_config(
            open_scene_ids=open_scene_ids,
            preview_by_series={
                series_id: tuple(scene_ids)
                for series_id, scene_ids in payload.preview_by_series.items()
            },
            learning_modules=dict(payload.learning_modules),
            expected_version=payload.expected_version,
            actor_id=str(admin.admin_user_id),
            now=clock(),
        )
        await audit_service.record(
            AuditEvent(
                actor_public_id=str(admin.admin_user_id),
                action="content.discovery-config.save",
                object_type="discovery_config",
                object_public_id="global",
                before_summary={"version": payload.expected_version},
                after_summary={"version": saved.version},
                reason=None,
                request_id=get_request_id(request),
                occurred_at=clock(),
            )
        )
        return _serialize_config(saved)

    @router.get("/revisions/{revision_id}/preview", response_model=AdminPreviewResponse)
    async def admin_preview(
        revision_id: str,
        response: Response,
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, object]:
        response.headers["Cache-Control"] = "no-store"
        return _serialize_preview(await service.admin_preview(revision_id))

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
        return _serialize_revision(revision)

    @router.post("/revisions/{revision_id}/publish-checks")
    async def validate_publish(
        revision_id: str,
        payload: PublishCheckRequest,
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, object]:
        revision = await service.get_revision(revision_id)
        summary = await service.inspect_publish(
            revision_id, frozenset(payload.acknowledged_warning_codes)
        )
        return {
            "revision_id": summary.revision_id,
            "version": revision.version,
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
            expected_version=payload.expected_version,
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
