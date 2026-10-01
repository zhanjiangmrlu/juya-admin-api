"""Authoring endpoints sharing one structured draft and fixed resource references."""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.infrastructure.observability.request_id import get_request_id
from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.audit.service import AuditEvent, AuditService
from juya_admin_api.modules.content.production_store import ProductionStore
from juya_admin_api.modules.content.router import (
    AdminDependency,
    RevisionResponse,
    SceneResponse,
    _serialize_revision,
    _serialize_scene,
)
from juya_admin_api.modules.content.schemas import SceneContent, SceneEntry
from juya_admin_api.modules.content.service import ContentService
from juya_admin_api.modules.media.service import MediaAdminService, MediaService
from juya_admin_api.shared.errors import AppError


class CreateSeriesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=100)
    slug: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    cover_asset_id: str | None = None


class CreateSceneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    series_id: str
    template_type: Literal["dialogue", "vocabulary"] = "dialogue"


class ImportImagesRequest(CreateSceneRequest):
    asset_ids: list[str] = Field(min_length=1, max_length=30)


class ImportImagesResponse(BaseModel):
    items: list[SceneResponse]


class LexiconWriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_type: Literal["VOCABULARY", "PHRASE"]
    entry: SceneEntry


class OcrAdoptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    expected_version: int = Field(ge=1)
    selected_fields: list[Literal["title_en", "title_zh", "dialogue", "vocabulary", "chunks"]] = (
        Field(min_length=1, max_length=5)
    )
    content: SceneContent


def create_production_content_router(
    store: ProductionStore,
    content: ContentService,
    media: MediaService,
    media_admin: MediaAdminService,
    *,
    audit_service: AuditService,
    current_admin: AdminDependency,
    current_admin_write: AdminDependency,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/content", tags=["admin-content"])

    async def audit(
        admin: SessionRecord,
        action: str,
        object_id: str,
        request: Request,
        summary: dict[str, object],
    ) -> None:
        await audit_service.record(
            AuditEvent(
                str(admin.admin_user_id),
                action,
                "CONTENT",
                object_id,
                {},
                summary,
                None,
                get_request_id(request),
                datetime.now(UTC),
            )
        )

    @router.post("/imports", response_model=ImportImagesResponse, status_code=201)
    async def import_images(
        payload: ImportImagesRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: Annotated[
            str, Header(alias="X-Idempotency-Key", min_length=1, max_length=128)
        ],
    ) -> dict[str, object]:
        ids = await store.import_images(
            payload.series_id,
            payload.template_type,
            payload.asset_ids,
            str(admin.admin_user_id),
            idempotency_key,
        )
        await audit(
            admin, "CONTENT_IMAGES_IMPORTED", payload.series_id, request, {"count": len(ids)}
        )
        return {"items": [_serialize_scene(await content.get_scene(scene_id)) for scene_id in ids]}

    @router.get("/series")
    async def list_series(
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, Any]:
        return {"items": await store.list_series()}

    @router.post("/series", status_code=201)
    async def create_series(
        payload: CreateSeriesRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: Annotated[
            str, Header(alias="X-Idempotency-Key", min_length=1, max_length=128)
        ],
    ) -> dict[str, Any]:
        result = await store.create_series(
            payload.title.strip(),
            payload.slug,
            payload.cover_asset_id,
            actor=str(admin.admin_user_id),
            key=idempotency_key,
        )
        await audit(
            admin, "CONTENT_SERIES_CREATED", result["id"], request, {"title": payload.title}
        )
        return result

    @router.post("/scenes", response_model=SceneResponse, status_code=201)
    async def create_scene(
        payload: CreateSceneRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: Annotated[
            str, Header(alias="X-Idempotency-Key", min_length=1, max_length=128)
        ],
    ) -> dict[str, object]:
        scene_id = await store.create_scene(
            payload.series_id,
            payload.template_type,
            actor=str(admin.admin_user_id),
            key=idempotency_key,
        )
        await audit(
            admin, "CONTENT_SCENE_CREATED", scene_id, request, {"series_id": payload.series_id}
        )
        return _serialize_scene(await content.get_scene(scene_id))

    @router.get("/lexicon")
    async def list_lexicon(
        _admin: Annotated[SessionRecord, Depends(current_admin)],
        query: Annotated[str, Query(max_length=500)] = "",
    ) -> dict[str, Any]:
        return {"items": await store.list_lexicon(query)}

    @router.post("/lexicon", response_model=SceneEntry, status_code=201)
    async def create_lexicon(
        payload: LexiconWriteRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> SceneEntry:
        result = await store.write_entry(
            payload.entry, payload.entry_type, str(admin.admin_user_id)
        )
        await audit(
            admin,
            "LEXICON_SAVED",
            result.entry_id,
            request,
            {"entry_version": result.entry_version},
        )
        return result

    @router.put("/lexicon/{entry_id}", response_model=SceneEntry)
    async def update_lexicon(
        entry_id: str,
        payload: LexiconWriteRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> SceneEntry:
        if payload.entry.entry_id != entry_id:
            raise AppError("ENTRY_REFERENCE_INVALID", "词条引用不匹配", 422)
        return await create_lexicon(payload, request, admin)

    @router.post("/revisions/{revision_id}/ocr-adoptions", response_model=RevisionResponse)
    async def adopt_ocr(
        revision_id: str,
        payload: OcrAdoptionRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, object]:
        candidate = await media_admin.get_ocr_candidate(payload.job_id)
        job = await media_admin.get_job(payload.job_id)
        revision = await content.get_revision(revision_id)
        if (
            job.input_payload.get("revision_id") != revision_id
            or job.input_payload.get("scene_id") != revision.scene_id
            or candidate.asset_id != revision.content.get("original_image_asset_id")
        ):
            raise AppError("OCR_REVISION_INVALID", "OCR 候选不属于当前草稿及原图", 409)
        if candidate.confirmed_revision_id is not None:
            raise AppError("OCR_ALREADY_ADOPTED", "OCR 候选已采纳", 409)
        merged = SceneContent.model_validate(revision.content).model_dump(mode="json")
        proposed = payload.content.model_dump(mode="json")
        for field in set(payload.selected_fields):
            merged[field] = proposed[field]
        saved = await content.save_revision(
            revision_id,
            merged,
            expected_version=payload.expected_version,
            actor_id=str(admin.admin_user_id),
        )
        await media_admin.confirm_ocr_candidate(
            payload.job_id, revision_id, actor_id=str(admin.admin_user_id), now=datetime.now(UTC)
        )
        await audit(
            admin,
            "OCR_ADOPTED",
            revision_id,
            request,
            {
                "job_id": payload.job_id,
                "version": saved.version,
                "selected_fields": payload.selected_fields,
            },
        )
        return _serialize_revision(saved)

    @router.get("/revisions/{revision_id}/resources/{resource_id}/signed-url")
    async def draft_resource(
        revision_id: str,
        resource_id: str,
        response: Response,
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, object]:
        revision = await content.get_revision(revision_id)
        fact = await store.resource(revision.scene_id, revision_id, resource_id, published=False)
        signed = await media.sign_media(str(fact["object_key"]), None, datetime.now(UTC))
        response.headers["Cache-Control"] = "no-store"
        return {"resource_id": resource_id, "url": signed.url, "expires_at": signed.expires_at}

    return router
