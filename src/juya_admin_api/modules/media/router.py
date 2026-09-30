from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.infrastructure.observability.request_id import get_request_id
from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.audit.service import AuditEvent, AuditService
from juya_admin_api.modules.content.service import ContentService
from juya_admin_api.modules.media.domain import (
    AudioTarget,
    AudioVersion,
    BatchJob,
    BatchJobItem,
    OcrCandidate,
    ProcessingJob,
    TrashEntry,
)
from juya_admin_api.modules.media.service import MediaAdminService, MediaService
from juya_admin_api.shared.errors import AppError


class UploadPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_type: str


class ConfirmUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_type: str
    object_key: str = Field(min_length=1, max_length=512)


class CreateOcrJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1, max_length=64)
    object_key: str = Field(min_length=1, max_length=512)
    series_id: str = Field(min_length=1, max_length=64)
    template_id: str = Field(min_length=1, max_length=64)


class OcrCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene_id: str | None = Field(default=None, max_length=64)
    content: dict[str, object] | None = None


class CreateAudioVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1, max_length=64)


class GenerateAudioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stable_key: str = Field(min_length=1, max_length=128)
    target_type: str = Field(min_length=1, max_length=32)
    text: str = Field(min_length=1, max_length=5000)
    voice: str = Field(min_length=1, max_length=64)


class RollbackAudioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_id: str = Field(min_length=1, max_length=64)


class CreateBatchJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_type: str = Field(min_length=1, max_length=32)
    target_ids: list[str] = Field(min_length=1, max_length=500)


class CreateTrashRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene_id: str = Field(min_length=1, max_length=64)
    revision_id: str = Field(min_length=1, max_length=64)


class EmptyCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProcessingJobResponse(BaseModel):
    id: str
    business_key: str
    job_type: str
    target_id: str
    batch_id: str | None
    status: str
    provider_request_id: str | None
    error_code: str | None
    created_by: str
    created_at: datetime
    updated_at: datetime
    cancel_requested_at: datetime | None


class OcrCandidateResponse(BaseModel):
    id: str
    job_id: str
    asset_id: str
    status: str
    template_type: str
    structured_candidate: dict[str, object]
    confidence: float | None
    error_code: str | None
    confirmed_revision_id: str | None


class OcrConfirmationResponse(BaseModel):
    revision_id: str
    revision_status: str
    version: int


class AudioTargetResponse(BaseModel):
    id: str
    stable_key: str
    target_type: str
    active_version_id: str | None


class AudioTargetListResponse(BaseModel):
    items: list[AudioTargetResponse]


class AudioVersionResponse(BaseModel):
    id: str
    target_id: str
    asset_id: str
    version_no: int
    source: str
    status: str
    provider_request_id: str | None
    processing_job_id: str | None
    created_by: str
    created_at: datetime


class AudioVersionListResponse(BaseModel):
    items: list[AudioVersionResponse]


class BatchJobItemResponse(BaseModel):
    id: str
    item_key: str
    target_id: str
    status: str
    attempt_count: int
    error_code: str | None
    result_version: int | None
    processing_job_id: str | None


class BatchJobResponse(BaseModel):
    id: str
    business_key: str
    job_type: str
    status: str
    total_count: int
    success_count: int
    failure_count: int
    created_by: str
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    cancel_requested_at: datetime | None
    items: list[BatchJobItemResponse]


class BatchJobPageResponse(BaseModel):
    items: list[BatchJobResponse]
    page: int
    page_size: int
    total: int


class TrashEntryResponse(BaseModel):
    id: str
    scene_id: str
    revision_id: str
    status: str
    trashed_by: str
    trashed_at: datetime
    retention_until: datetime
    restored_at: datetime | None
    cleaned_at: datetime | None


class TrashListResponse(BaseModel):
    items: list[TrashEntryResponse]


AdminDependency = Callable[..., Awaitable[SessionRecord]]
SignedTargetResolver = Callable[[str, str, datetime], Awaitable[tuple[str, datetime | None]]]
IdempotencyKey = Annotated[
    str,
    Header(alias="X-Idempotency-Key", min_length=1, max_length=128),
]


class MediaTaskDispatcher(Protocol):
    async def enqueue_ocr(self, job_id: str, object_key: str, template_type: str) -> None: ...

    async def enqueue_tts(
        self,
        job_id: str,
        stable_key: str,
        target_type: str,
        text: str,
        voice: str,
    ) -> None: ...


def create_media_router(
    service: MediaService,
    *,
    current_admin_write: AdminDependency,
    admin_service: MediaAdminService | None = None,
    content_service: ContentService | None = None,
    audit_service: AuditService | None = None,
    task_dispatcher: MediaTaskDispatcher | None = None,
    current_admin: AdminDependency | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/media", tags=["admin-media"])
    read_admin = current_admin or current_admin_write

    def media_admin() -> MediaAdminService:
        if admin_service is None:
            raise AppError("MEDIA_ADMIN_UNAVAILABLE", "媒体任务管理能力不可用", 503)
        return admin_service

    def content_admin() -> ContentService:
        if content_service is None:
            raise AppError("CONTENT_ADMIN_UNAVAILABLE", "内容管理能力不可用", 503)
        return content_service

    def dispatcher() -> MediaTaskDispatcher:
        if task_dispatcher is None:
            raise AppError("MEDIA_TASKS_UNAVAILABLE", "媒体任务调度能力不可用", 503)
        return task_dispatcher

    async def audit(
        request: Request,
        admin: SessionRecord,
        action: str,
        object_type: str,
        object_id: str,
        after: dict[str, object],
    ) -> None:
        if audit_service is None:
            return
        await audit_service.record(
            AuditEvent(
                actor_public_id=str(admin.admin_user_id),
                action=action,
                object_type=object_type,
                object_public_id=object_id,
                before_summary={},
                after_summary=after,
                reason=None,
                request_id=get_request_id(request),
                occurred_at=clock(),
            )
        )

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

    @router.post("/ocr/jobs", status_code=201, response_model=ProcessingJobResponse)
    async def create_ocr_job(
        payload: CreateOcrJobRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: IdempotencyKey,
    ) -> ProcessingJobResponse:
        job = await media_admin().create_job(
            business_key=f"ocr:{admin.admin_user_id}:{idempotency_key}",
            job_type="OCR",
            target_id=payload.asset_id,
            actor_id=str(admin.admin_user_id),
            now=clock(),
            input_payload=payload.model_dump(),
        )
        await dispatcher().enqueue_ocr(job.id, payload.object_key, payload.template_id)
        return _job_response(job)

    @router.get("/ocr/jobs/{job_id}", response_model=ProcessingJobResponse)
    async def get_ocr_job(
        job_id: str,
        _admin: Annotated[SessionRecord, Depends(read_admin)],
    ) -> ProcessingJobResponse:
        return _job_response(await media_admin().get_job(job_id))

    @router.get("/ocr/jobs/{job_id}/candidate", response_model=OcrCandidateResponse)
    async def get_ocr_candidate(
        job_id: str,
        _admin: Annotated[SessionRecord, Depends(read_admin)],
    ) -> OcrCandidateResponse:
        return _candidate_response(await media_admin().get_ocr_candidate(job_id))

    @router.post("/ocr/jobs/{job_id}/commands/{operation}")
    async def command_ocr_job(
        job_id: str,
        operation: str,
        payload: OcrCommandRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: IdempotencyKey,
    ) -> ProcessingJobResponse | OcrConfirmationResponse:
        media = media_admin()
        if operation == "cancel":
            job = await media.cancel_job(job_id, now=clock())
            await audit(
                request, admin, "media.ocr.cancel", "processing_job", job.id, {"status": job.status}
            )
            return _job_response(job)
        if operation == "retry":
            original = await media.get_job(job_id)
            if original.status not in {"FAILED", "CANCELLED"}:
                raise AppError("MEDIA_JOB_NOT_RETRYABLE", "当前媒体任务不可重试", 409)
            retried = await media.create_job(
                business_key=f"retry:{original.id}:{idempotency_key}",
                job_type=original.job_type,
                target_id=original.target_id,
                actor_id=str(admin.admin_user_id),
                now=clock(),
                batch_id=original.batch_id,
                input_payload=original.input_payload,
            )
            object_key = str(original.input_payload.get("object_key", ""))
            template_id = str(original.input_payload.get("template_id", ""))
            if not object_key or not template_id:
                raise AppError("MEDIA_JOB_INPUT_INVALID", "媒体任务输入不完整", 409)
            await dispatcher().enqueue_ocr(retried.id, object_key, template_id)
            await audit(
                request,
                admin,
                "media.ocr.retry",
                "processing_job",
                retried.id,
                {"status": retried.status},
            )
            return _job_response(retried)
        if operation != "confirm":
            raise AppError("MEDIA_JOB_OPERATION_INVALID", "媒体任务操作不支持", 422)
        if payload.scene_id is None or payload.content is None:
            raise AppError("OCR_CONFIRMATION_INVALID", "确认 OCR 候选需要场景和内容", 422)
        candidate = await media.get_ocr_candidate(job_id)
        content = content_admin()
        if candidate.confirmed_revision_id is not None:
            revision = await content.get_revision(candidate.confirmed_revision_id)
        else:
            scene = await content.get_scene(payload.scene_id)
            revision = await content.create_revision(
                scene.id,
                scene.draft_revision_id,
                str(admin.admin_user_id),
                clock(),
            )
            revision = await content.save_revision(
                revision.id,
                payload.content,
                expected_version=revision.version,
                actor_id=str(admin.admin_user_id),
            )
            await media.confirm_ocr_candidate(
                job_id,
                revision.id,
                actor_id=str(admin.admin_user_id),
                now=clock(),
            )
            await audit(
                request,
                admin,
                "media.ocr.confirm",
                "scene_revision",
                revision.id,
                {"status": revision.status},
            )
        return OcrConfirmationResponse(
            revision_id=revision.id,
            revision_status=revision.status,
            version=revision.version,
        )

    @router.get("/audio-targets", response_model=AudioTargetListResponse)
    async def list_audio_targets(
        _admin: Annotated[SessionRecord, Depends(read_admin)],
        scene_id: Annotated[str | None, Query(max_length=64)] = None,
    ) -> AudioTargetListResponse:
        del scene_id
        return AudioTargetListResponse(
            items=[
                _audio_target_response(item) for item in await media_admin().list_audio_targets()
            ]
        )

    @router.get(
        "/audio-targets/{target_id}/versions",
        response_model=AudioVersionListResponse,
    )
    async def list_audio_versions(
        target_id: str,
        _admin: Annotated[SessionRecord, Depends(read_admin)],
    ) -> AudioVersionListResponse:
        versions = await media_admin().list_audio_versions(target_id)
        return AudioVersionListResponse(items=[_audio_version_response(item) for item in versions])

    @router.post(
        "/audio-targets/{target_id}/versions",
        status_code=201,
        response_model=AudioVersionResponse,
    )
    async def create_audio_version(
        target_id: str,
        payload: CreateAudioVersionRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: IdempotencyKey,
    ) -> AudioVersionResponse:
        media = media_admin()
        target = await media.get_audio_target(target_id)
        version = await media.create_audio_candidate(
            stable_key=target.stable_key,
            target_type=target.target_type,
            asset_id=payload.asset_id,
            source="MANUAL",
            actor_id=str(admin.admin_user_id),
            now=clock(),
            provider_request_id=f"manual:{target.id}:{idempotency_key}",
        )
        await audit(
            request,
            admin,
            "media.audio.upload",
            "audio_version",
            version.id,
            {"status": version.status},
        )
        return _audio_version_response(version)

    @router.post(
        "/audio-targets/{target_id}/commands/generate",
        status_code=201,
        response_model=ProcessingJobResponse,
    )
    async def generate_audio(
        target_id: str,
        payload: GenerateAudioRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: IdempotencyKey,
    ) -> ProcessingJobResponse:
        target = await media_admin().get_audio_target(target_id)
        if target.stable_key != payload.stable_key or target.target_type != payload.target_type:
            raise AppError("AUDIO_TARGET_MISMATCH", "音频目标信息不匹配", 409)
        job = await media_admin().create_job(
            business_key=f"tts:{target.id}:{idempotency_key}",
            job_type="TTS",
            target_id=target.id,
            actor_id=str(admin.admin_user_id),
            now=clock(),
            input_payload=payload.model_dump(),
        )
        await dispatcher().enqueue_tts(
            job.id,
            payload.stable_key,
            payload.target_type,
            payload.text,
            payload.voice,
        )
        await audit(
            request, admin, "media.audio.generate", "processing_job", job.id, {"status": job.status}
        )
        return _job_response(job)

    @router.post(
        "/audio-versions/{version_id}/commands/confirm",
        response_model=AudioTargetResponse,
    )
    async def confirm_audio_version(
        version_id: str,
        _payload: EmptyCommandRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        _idempotency_key: IdempotencyKey,
    ) -> AudioTargetResponse:
        target = await media_admin().confirm_audio_version(
            version_id, actor_id=str(admin.admin_user_id), now=clock()
        )
        await audit(
            request,
            admin,
            "media.audio.confirm",
            "audio_target",
            target.id,
            {"active_version_id": target.active_version_id or ""},
        )
        return _audio_target_response(target)

    @router.post(
        "/audio-targets/{target_id}/commands/rollback",
        response_model=AudioTargetResponse,
    )
    async def rollback_audio_version(
        target_id: str,
        payload: RollbackAudioRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        _idempotency_key: IdempotencyKey,
    ) -> AudioTargetResponse:
        target = await media_admin().rollback_audio_version(
            target_id,
            payload.version_id,
            actor_id=str(admin.admin_user_id),
            now=clock(),
        )
        await audit(
            request,
            admin,
            "media.audio.rollback",
            "audio_target",
            target.id,
            {"active_version_id": target.active_version_id or ""},
        )
        return _audio_target_response(target)

    @router.get("/batch-jobs", response_model=BatchJobPageResponse)
    async def list_batch_jobs(
        _admin: Annotated[SessionRecord, Depends(read_admin)],
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> BatchJobPageResponse:
        batches = await media_admin().list_batches()
        start = (page - 1) * page_size
        return BatchJobPageResponse(
            items=[
                await _batch_response(media_admin(), item)
                for item in batches[start : start + page_size]
            ],
            page=page,
            page_size=page_size,
            total=len(batches),
        )

    @router.post("/batch-jobs", status_code=201, response_model=BatchJobResponse)
    async def create_batch_job(
        payload: CreateBatchJobRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: IdempotencyKey,
    ) -> BatchJobResponse:
        batch = await media_admin().create_batch(
            business_key=f"batch:{admin.admin_user_id}:{idempotency_key}",
            job_type=payload.job_type,
            target_ids=tuple(payload.target_ids),
            actor_id=str(admin.admin_user_id),
            now=clock(),
        )
        await audit(
            request, admin, "media.batch.create", "batch_job", batch.id, {"status": batch.status}
        )
        return await _batch_response(media_admin(), batch)

    @router.get("/batch-jobs/{batch_id}", response_model=BatchJobResponse)
    async def get_batch_job(
        batch_id: str,
        _admin: Annotated[SessionRecord, Depends(read_admin)],
    ) -> BatchJobResponse:
        media = media_admin()
        return await _batch_response(media, await media.get_batch(batch_id))

    @router.post("/batch-jobs/{batch_id}/commands/{operation}", response_model=BatchJobResponse)
    async def command_batch_job(
        batch_id: str,
        operation: str,
        _payload: EmptyCommandRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: IdempotencyKey,
    ) -> BatchJobResponse:
        media = media_admin()
        if operation == "cancel":
            batch = await media.cancel_batch(batch_id, now=clock())
            action = "media.batch.cancel"
        elif operation == "retry-failed":
            original = await media.get_batch(batch_id)
            items = await media.list_batch_items(batch_id)
            target_ids = tuple(item.target_id for item in items if item.status == "FAILED")
            if not target_ids:
                raise AppError("BATCH_RETRY_EMPTY", "批量任务没有可重试失败项", 409)
            batch = await media.create_batch(
                business_key=f"batch-retry:{original.id}:{idempotency_key}",
                job_type=original.job_type,
                target_ids=target_ids,
                actor_id=str(admin.admin_user_id),
                now=clock(),
            )
            action = "media.batch.retry"
        else:
            raise AppError("BATCH_OPERATION_INVALID", "批量任务操作不支持", 422)
        await audit(request, admin, action, "batch_job", batch.id, {"status": batch.status})
        return await _batch_response(media, batch)

    @router.get("/trash", response_model=TrashListResponse)
    async def list_trash(
        _admin: Annotated[SessionRecord, Depends(read_admin)],
    ) -> TrashListResponse:
        return TrashListResponse(
            items=[_trash_response(item) for item in await media_admin().list_trash_entries()]
        )

    @router.post("/trash", status_code=201, response_model=TrashEntryResponse)
    async def create_trash_entry(
        payload: CreateTrashRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        _idempotency_key: IdempotencyKey,
    ) -> TrashEntryResponse:
        entry = await media_admin().trash_draft(
            payload.scene_id,
            payload.revision_id,
            actor_id=str(admin.admin_user_id),
            now=clock(),
        )
        await audit(
            request, admin, "media.trash.create", "trash_entry", entry.id, {"status": entry.status}
        )
        return _trash_response(entry)

    @router.post(
        "/trash/{entry_id}/commands/{operation}",
        response_model=TrashEntryResponse,
    )
    async def command_trash_entry(
        entry_id: str,
        operation: str,
        _payload: EmptyCommandRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        _idempotency_key: IdempotencyKey,
    ) -> TrashEntryResponse:
        media = media_admin()
        if operation == "restore":
            entry = await media.restore_draft(
                entry_id, actor_id=str(admin.admin_user_id), now=clock()
            )
            action = "media.trash.restore"
        elif operation == "cleanup":
            entry = await media.cleanup_draft(
                entry_id, actor_id=str(admin.admin_user_id), now=clock()
            )
            action = "media.trash.cleanup"
        else:
            raise AppError("TRASH_OPERATION_INVALID", "回收站操作不支持", 422)
        await audit(request, admin, action, "trash_entry", entry.id, {"status": entry.status})
        return _trash_response(entry)

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


def _job_response(job: ProcessingJob) -> ProcessingJobResponse:
    return ProcessingJobResponse(
        **{name: getattr(job, name) for name in ProcessingJobResponse.model_fields}
    )


def _candidate_response(candidate: OcrCandidate) -> OcrCandidateResponse:
    return OcrCandidateResponse(
        **{name: getattr(candidate, name) for name in OcrCandidateResponse.model_fields}
    )


def _audio_target_response(target: AudioTarget) -> AudioTargetResponse:
    return AudioTargetResponse(
        **{name: getattr(target, name) for name in AudioTargetResponse.model_fields}
    )


def _audio_version_response(version: AudioVersion) -> AudioVersionResponse:
    return AudioVersionResponse(
        **{name: getattr(version, name) for name in AudioVersionResponse.model_fields}
    )


def _batch_item_response(item: BatchJobItem) -> BatchJobItemResponse:
    return BatchJobItemResponse(
        **{name: getattr(item, name) for name in BatchJobItemResponse.model_fields}
    )


async def _batch_response(service: MediaAdminService, batch: BatchJob) -> BatchJobResponse:
    values = {
        name: getattr(batch, name) for name in BatchJobResponse.model_fields if name != "items"
    }
    values["items"] = [
        _batch_item_response(item) for item in await service.list_batch_items(batch.id)
    ]
    return BatchJobResponse(**values)


def _trash_response(entry: TrashEntry) -> TrashEntryResponse:
    return TrashEntryResponse(
        **{name: getattr(entry, name) for name in TrashEntryResponse.model_fields}
    )
