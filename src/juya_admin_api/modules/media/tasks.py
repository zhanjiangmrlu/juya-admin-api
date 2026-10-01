import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol, cast

from sqlalchemy.ext.asyncio import AsyncEngine

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.infrastructure.db.session import create_engine, create_session_factory
from juya_admin_api.infrastructure.tasks.celery_app import celery_app
from juya_admin_api.integrations.ocr.baidu import create_ocr_provider
from juya_admin_api.integrations.ocr.protocol import OcrProvider, OcrResult
from juya_admin_api.integrations.oss.aliyun import AliyunOssProvider
from juya_admin_api.integrations.oss.credentials import ControlledCredentialsProvider
from juya_admin_api.integrations.oss.provider import validate_object_key
from juya_admin_api.integrations.tts.protocol import TtsProvider, TtsResult
from juya_admin_api.modules.media.domain import ProcessingJob as PersistentProcessingJob
from juya_admin_api.modules.media.quota import OcrQuotaService, SQLAlchemyOcrQuotaRepository
from juya_admin_api.modules.media.repository import (
    SQLAlchemyMediaAdminRepository,
    SQLAlchemyMediaRepository,
)
from juya_admin_api.modules.media.service import (
    MediaAdminRepository,
    MediaAdminService,
    MediaService,
)
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


@dataclass(frozen=True, slots=True)
class ProcessingJob:
    id: str
    business_key: str
    job_type: str
    target_id: str
    status: str
    provider_request_id: str | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AudioTarget:
    target_id: str
    object_key: str
    source: str
    revision: int


@dataclass(frozen=True, slots=True)
class BatchJob:
    id: str
    business_key: str
    job_type: str
    status: str
    total_count: int
    success_count: int
    failure_count: int
    created_at: datetime


class MediaJobRepository(Protocol):
    async def get_job(self, business_key: str) -> ProcessingJob | None: ...

    async def save_job(self, job: ProcessingJob) -> ProcessingJob: ...

    async def save_ocr_candidate(self, job_id: str, result: OcrResult) -> None: ...

    async def save_tts_candidate(self, job_id: str, target_id: str, result: TtsResult) -> None: ...

    async def get_batch(self, business_key: str) -> BatchJob | None: ...

    async def save_batch(self, batch: BatchJob) -> BatchJob: ...


class InMemoryMediaJobRepository:
    def __init__(self) -> None:
        self.jobs: dict[str, ProcessingJob] = {}
        self.ocr_candidates: dict[str, OcrResult] = {}
        self.tts_candidates: dict[str, TtsResult] = {}
        self.audio_targets: dict[str, AudioTarget] = {}
        self.batches: dict[str, BatchJob] = {}

    async def get_job(self, business_key: str) -> ProcessingJob | None:
        return self.jobs.get(business_key)

    async def save_job(self, job: ProcessingJob) -> ProcessingJob:
        self.jobs[job.business_key] = job
        return job

    async def save_ocr_candidate(self, job_id: str, result: OcrResult) -> None:
        self.ocr_candidates[job_id] = result

    async def save_tts_candidate(self, job_id: str, target_id: str, result: TtsResult) -> None:
        self.tts_candidates[job_id] = result
        current = self.audio_targets.get(target_id)
        if current is None or current.source != "MANUAL":
            revision = 1 if current is None else current.revision + 1
            self.audio_targets[target_id] = AudioTarget(
                target_id, result.object_key, "TTS", revision
            )

    async def get_batch(self, business_key: str) -> BatchJob | None:
        return self.batches.get(business_key)

    async def save_batch(self, batch: BatchJob) -> BatchJob:
        self.batches[batch.business_key] = batch
        return batch

    def set_manual_audio(self, target_id: str, object_key: str, revision: int) -> None:
        self.audio_targets[target_id] = AudioTarget(target_id, object_key, "MANUAL", revision)


class MediaTaskService:
    def __init__(
        self,
        repository: MediaJobRepository,
        ocr: OcrProvider,
        tts: TtsProvider,
    ) -> None:
        self._repository = repository
        self._ocr = ocr
        self._tts = tts

    async def run_ocr(
        self,
        business_key: str,
        asset_id: str,
        object_key: str,
        now: datetime,
        *,
        template_type: str = "default",
    ) -> ProcessingJob:
        existing = await self._repository.get_job(business_key)
        if existing is not None:
            return existing
        job = ProcessingJob(
            new_ulid(now), business_key, "OCR", asset_id, "RUNNING", None, None, now, now
        )
        await self._repository.save_job(job)
        try:
            result = await self._ocr.recognize(object_key, template_type)
        except Exception:  # Provider exceptions are normalized at this boundary.
            return await self._repository.save_job(
                replace(job, status="FAILED", error_code="OCR_PROVIDER_FAILED")
            )
        await self._repository.save_ocr_candidate(job.id, result)
        return await self._repository.save_job(
            replace(job, status="SUCCEEDED", provider_request_id=result.provider_request_id)
        )

    async def run_tts(
        self,
        business_key: str,
        target_id: str,
        text: str,
        voice: str,
        now: datetime,
    ) -> ProcessingJob:
        existing = await self._repository.get_job(business_key)
        if existing is not None:
            return existing
        return await self._repository.save_job(
            ProcessingJob(
                new_ulid(now),
                business_key,
                "TTS",
                target_id,
                "FAILED",
                None,
                "TTS_DISABLED",
                now,
                now,
            )
        )

    async def run_tts_batch(
        self,
        business_key: str,
        items: tuple[tuple[str, str, str], ...],
        now: datetime,
    ) -> BatchJob:
        existing = await self._repository.get_batch(business_key)
        if existing is not None:
            return existing
        success_count = 0
        for index, (target_id, text, voice) in enumerate(items):
            item = await self.run_tts(
                f"{business_key}:{index}:{target_id}", target_id, text, voice, now
            )
            success_count += item.status == "SUCCEEDED"
        batch = BatchJob(
            id=new_ulid(now),
            business_key=business_key,
            job_type="TTS",
            status="COMPLETED" if success_count == len(items) else "COMPLETED_WITH_ERRORS",
            total_count=len(items),
            success_count=success_count,
            failure_count=len(items) - success_count,
            created_at=now,
        )
        return await self._repository.save_batch(batch)


class PersistentMediaTaskService:
    def __init__(
        self,
        admin_service: MediaAdminService,
        repository: MediaAdminRepository,
        ocr: OcrProvider,
        tts: TtsProvider,
        *,
        register_generated_audio: Callable[[TtsResult, datetime], Awaitable[str]],
        quota: OcrQuotaService | None = None,
        media_service: MediaService | None = None,
    ) -> None:
        self._admin_service = admin_service
        self._repository = repository
        self._ocr = ocr
        self._tts = tts
        self._register_generated_audio = register_generated_audio
        self._quota = quota
        self._media_service = media_service

    async def run_ocr(
        self,
        job_id: str,
        object_key: str,
        template_type: str,
        now: datetime,
    ) -> PersistentProcessingJob:
        job = await self._admin_service.get_job(job_id)
        if not await self._repository.claim_job(job.id, now):
            return await self._admin_service.get_job(job.id)
        try:
            if self._quota is not None:
                await self._quota.reserve(job.id, now)
        except AppError as error:
            return await self._admin_service.save_job_result(
                job.id, status="FAILED", provider_request_id=None, error_code=error.code, now=now
            )
        try:
            validate_object_key(object_key)
            if not object_key.startswith(f"uploads/images/{job.created_by}/"):
                raise AppError("OCR_OBJECT_INVALID", "OCR素材对象不属于任务上传者", 422)
            if job.input_payload.get("object_key", object_key) != object_key:
                raise AppError("OCR_OBJECT_INVALID", "OCR素材对象与任务不一致", 422)
        except AppError:
            return await self._admin_service.save_job_result(
                job.id,
                status="FAILED",
                provider_request_id=None,
                error_code="OCR_OBJECT_INVALID",
                now=now,
            )
        try:
            if self._media_service is not None:
                asset = await self._media_service.get_asset(job.target_id)
                if asset.object_key != object_key or asset.asset_type != "images":
                    raise AppError("OCR_OBJECT_INVALID", "OCR对象与素材不一致", 422)
                await self._media_service.read_asset_bytes(asset)
            result = await self._ocr.recognize(object_key, template_type)
        except Exception:
            return await self._admin_service.save_job_result(
                job.id,
                status="FAILED",
                provider_request_id=None,
                error_code="OCR_PROVIDER_FAILED",
                now=now,
            )
        current = await self._admin_service.get_job(job.id)
        if current.status == "CANCELLED":
            return current
        await self._admin_service.record_ocr_candidate(
            job.id,
            provider_request_id=result.provider_request_id,
            text_value=result.text,
            blocks=cast(list[dict[str, object]], result.blocks),
            template_type=template_type,
            now=now,
        )
        return await self._admin_service.save_job_result(
            job.id,
            status="SUCCEEDED",
            provider_request_id=result.provider_request_id,
            error_code=None,
            now=now,
        )

    async def run_tts(
        self,
        job_id: str,
        *,
        stable_key: str,
        target_type: str,
        text: str,
        voice: str,
        now: datetime,
    ) -> PersistentProcessingJob:
        job = await self._admin_service.get_job(job_id)
        if job.status in {"SUCCEEDED", "CANCELLED", "FAILED"}:
            return job
        return await self._admin_service.save_job_result(
            job.id, status="FAILED", provider_request_id=None, error_code="TTS_DISABLED", now=now
        )


class CeleryMediaTaskDispatcher:
    def __init__(self, *, enabled: bool) -> None:
        self._enabled = enabled

    async def enqueue_batch(self, batch_id: str) -> None:
        celery_app.send_task("juya.content.publish.batch_execute", kwargs={"batch_id": batch_id})

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise AppError(
                "MEDIA_TASKS_UNAVAILABLE",
                "当前环境未配置 OCR/TTS 任务提供方",
                503,
            )

    async def enqueue_ocr(self, job_id: str, object_key: str, template_type: str) -> None:
        self._require_enabled()
        celery_app.send_task(
            "juya.content.ocr.process",
            kwargs={
                "job_id": job_id,
                "object_key": object_key,
                "template_type": template_type,
            },
        )

    async def enqueue_tts(
        self,
        job_id: str,
        stable_key: str,
        target_type: str,
        text: str,
        voice: str,
    ) -> None:
        raise AppError("TTS_DISABLED", "本期仅支持人工上传音频, TTS执行已禁用", 409)


class LocalOcrProvider:
    async def recognize(self, object_key: str, template_type: str) -> OcrResult:
        digest = hashlib.sha256(f"{object_key}:{template_type}".encode()).hexdigest()[:24]
        return OcrResult(
            provider_request_id=f"local-ocr-{digest}",
            text=f"recognized:{object_key}:{template_type}",
            blocks=[
                {
                    "type": "text",
                    "text": f"recognized:{object_key}",
                    "confidence": 1.0,
                }
            ],
        )


class LocalTtsProvider:
    async def synthesize(self, audio_target: str, voice: str, text: str) -> TtsResult:
        digest = hashlib.sha256(f"{audio_target}:{voice}:{text}".encode()).hexdigest()
        return TtsResult(
            provider_request_id=f"local-tts-{digest[:24]}",
            object_key=f"generated/audio/{digest}.mp3",
            duration_ms=max(len(text) * 100, 100),
        )


@celery_app.task(name="juya.content.ocr.process")  # type: ignore[untyped-decorator]
def process_ocr(job_id: str, object_key: str, template_type: str) -> dict[str, object]:
    return asyncio.run(_process_ocr(job_id, object_key, template_type))


@celery_app.task(name="juya.content.audio.generate")  # type: ignore[untyped-decorator]
def process_tts(
    job_id: str,
    stable_key: str,
    target_type: str,
    text: str,
    voice: str,
) -> dict[str, object]:
    return asyncio.run(_process_tts(job_id, stable_key, target_type, text, voice))


async def _process_ocr(
    job_id: str,
    object_key: str,
    template_type: str,
) -> dict[str, object]:
    worker, engine = _local_worker()
    try:
        job = await worker.run_ocr(job_id, object_key, template_type, datetime.now(UTC))
        return _job_payload(job)
    finally:
        await engine.dispose()


async def _process_tts(
    job_id: str,
    stable_key: str,
    target_type: str,
    text: str,
    voice: str,
) -> dict[str, object]:
    worker, engine = _local_worker()
    try:
        job = await worker.run_tts(
            job_id,
            stable_key=stable_key,
            target_type=target_type,
            text=text,
            voice=voice,
            now=datetime.now(UTC),
        )
        return _job_payload(job)
    finally:
        await engine.dispose()


def _local_worker() -> tuple[PersistentMediaTaskService, AsyncEngine]:
    settings = Settings()
    if settings.ocr_provider != "baidu":
        raise RuntimeError("OCR provider is disabled")
    settings.validate_oss_configuration()
    if settings.database_url is None:
        raise RuntimeError("JUYA_DATABASE_URL is required for media tasks")
    database_url = settings.database_url.get_secret_value().replace(
        "mysql+pymysql://", "mysql+asyncmy://", 1
    )
    engine = create_engine(database_url)
    sessions = create_session_factory(engine)
    admin_repository = SQLAlchemyMediaAdminRepository(sessions)
    asset_repository = SQLAlchemyMediaRepository(sessions)

    assert settings.oss_region is not None and settings.oss_bucket is not None
    oss = AliyunOssProvider(
        settings.oss_region,
        settings.oss_bucket,
        endpoint=settings.oss_endpoint,
        credentials_provider=ControlledCredentialsProvider(
            mode=settings.oss_credentials_mode,
            role_name=settings.oss_ram_role_name,
            access_key_id=settings.oss_access_key_id.get_secret_value()
            if settings.oss_access_key_id
            else None,
            access_key_secret=settings.oss_access_key_secret.get_secret_value()
            if settings.oss_access_key_secret
            else None,
            security_token=settings.oss_session_token.get_secret_value()
            if settings.oss_session_token
            else None,
            expires_at=settings.oss_credentials_expires_at,
            from_environment=True,
        ),
    )

    async def disabled_audio(result: TtsResult, now: datetime) -> str:
        raise AppError("TTS_DISABLED", "TTS执行已禁用", 409)

    return (
        PersistentMediaTaskService(
            MediaAdminService(admin_repository),
            admin_repository,
            create_ocr_provider(settings, oss),
            LocalTtsProvider(),
            register_generated_audio=disabled_audio,
            quota=OcrQuotaService(SQLAlchemyOcrQuotaRepository(sessions)),
            media_service=MediaService(oss, asset_repository, ffprobe_path=settings.ffprobe_path),
        ),
        engine,
    )


def _job_payload(job: PersistentProcessingJob) -> dict[str, object]:
    return {
        "id": job.id,
        "status": job.status,
        "provider_request_id": job.provider_request_id,
        "error_code": job.error_code,
    }


@celery_app.task(name="juya.content.publish.batch_execute")  # type: ignore[untyped-decorator]
def process_batch(batch_id: str) -> dict[str, object]:
    return asyncio.run(_process_batch(batch_id))


async def _process_batch(batch_id: str) -> dict[str, object]:
    from juya_admin_api.modules.audit.service import (
        AuditEvent,
        AuditService,
        SQLAlchemyAuditRepository,
    )
    from juya_admin_api.modules.content.batch_executor import BatchExecutor, ContentBatchOperations
    from juya_admin_api.modules.content.production_store import ProductionStore
    from juya_admin_api.modules.content.repository import SQLAlchemyContentRepository
    from juya_admin_api.modules.content.service import ContentService

    settings = Settings()
    if settings.database_url is None:
        raise RuntimeError("JUYA_DATABASE_URL is required for batch tasks")
    engine = create_engine(
        settings.database_url.get_secret_value().replace("mysql+pymysql://", "mysql+asyncmy://", 1)
    )
    sessions = create_session_factory(engine)
    repository = SQLAlchemyMediaAdminRepository(sessions)
    admin = MediaAdminService(repository)
    content = ContentService(SQLAlchemyContentRepository(sessions))
    batch = await admin.get_batch(batch_id)

    async def run_ocr(
        kind: str, target: str, payload: dict[str, object], actor: str, key: str, now: datetime
    ) -> dict[str, object]:
        scene = await content.get_scene(target)
        if scene.draft_revision_id is None:
            raise AppError("OCR_DRAFT_MISMATCH", "OCR需要当前场景草稿", 409)
        revision = await content.get_revision(scene.draft_revision_id)
        asset_id = revision.content.get("original_image_asset_id")
        if not isinstance(asset_id, str):
            raise AppError("OCR_IMAGE_REQUIRED", "OCR需要学习原图", 409)
        worker, worker_engine = _local_worker()
        try:
            asset = await SQLAlchemyMediaRepository(sessions).get(asset_id)
            if asset is None or asset.status != "CONFIRMED" or asset.security_status != "PASSED":
                raise AppError("MEDIA_ASSET_UNAVAILABLE", "学习原图未确认", 409)
            job = await admin.create_job(
                business_key=f"batch-ocr:{key}",
                job_type="OCR",
                target_id=asset_id,
                actor_id=actor,
                now=now,
                batch_id=batch_id,
                input_payload={
                    "object_key": asset.object_key,
                    "template_id": str(payload.get("template_id", "dialogue")),
                    "scene_id": target,
                    "revision_id": revision.id,
                },
            )
            result = await worker.run_ocr(
                job.id, asset.object_key, str(payload.get("template_id", "dialogue")), now
            )
            if result.status != "SUCCEEDED":
                raise AppError(result.error_code or "OCR_PROVIDER_FAILED", "OCR失败", 409)
            return {"scene_id": target, "job_id": job.id, "asset_id": asset_id}
        finally:
            await worker_engine.dispose()

    async def audit(kind: str, target: str, result: dict[str, object], now: datetime) -> None:
        await AuditService(SQLAlchemyAuditRepository(sessions)).record(
            AuditEvent(
                actor_public_id=batch.created_by,
                action=f"media.batch.{kind.lower()}",
                object_type="scene",
                object_public_id=target,
                before_summary={},
                after_summary=result,
                reason=None,
                request_id=f"batch-{batch_id}",
                occurred_at=now,
            )
        )

    try:
        result = await BatchExecutor(
            admin,
            repository,
            ContentBatchOperations(content, ProductionStore(sessions), ocr=run_ocr),
            audit=audit,
        ).run(batch_id, datetime.now(UTC))
        return {
            "id": result.id,
            "status": result.status,
            "success_count": result.success_count,
            "failure_count": result.failure_count,
        }
    finally:
        await engine.dispose()
