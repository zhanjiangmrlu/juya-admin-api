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
from juya_admin_api.integrations.ocr.protocol import OcrProvider, OcrResult
from juya_admin_api.integrations.oss.provider import validate_object_key
from juya_admin_api.integrations.tts.protocol import TtsProvider, TtsResult
from juya_admin_api.modules.media.domain import ProcessingJob as PersistentProcessingJob
from juya_admin_api.modules.media.repository import (
    SQLAlchemyMediaAdminRepository,
    SQLAlchemyMediaRepository,
)
from juya_admin_api.modules.media.service import (
    MediaAdminRepository,
    MediaAdminService,
    MediaAsset,
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
        job = ProcessingJob(
            new_ulid(now), business_key, "TTS", target_id, "RUNNING", None, None, now, now
        )
        await self._repository.save_job(job)
        if not text.strip():
            return await self._repository.save_job(
                replace(job, status="FAILED", error_code="TTS_TEXT_EMPTY")
            )
        try:
            result = await self._tts.synthesize(target_id, voice, text)
        except Exception:  # Provider exceptions are normalized at this boundary.
            return await self._repository.save_job(
                replace(job, status="FAILED", error_code="TTS_PROVIDER_FAILED")
            )
        await self._repository.save_tts_candidate(job.id, target_id, result)
        return await self._repository.save_job(
            replace(job, status="SUCCEEDED", provider_request_id=result.provider_request_id)
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
    ) -> None:
        self._admin_service = admin_service
        self._repository = repository
        self._ocr = ocr
        self._tts = tts
        self._register_generated_audio = register_generated_audio

    async def run_ocr(
        self,
        job_id: str,
        object_key: str,
        template_type: str,
        now: datetime,
    ) -> PersistentProcessingJob:
        job = await self._admin_service.get_job(job_id)
        if job.status in {"SUCCEEDED", "CANCELLED"}:
            return job
        existing_candidate = await self._repository.get_ocr_candidate_by_job(job.id)
        if existing_candidate is not None:
            return await self._admin_service.save_job_result(
                job.id,
                status="SUCCEEDED",
                provider_request_id=existing_candidate.provider_request_id,
                error_code=None,
                now=now,
            )
        await self._admin_service.save_job_result(
            job.id,
            status="RUNNING",
            provider_request_id=None,
            error_code=None,
            now=now,
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
        if job.status in {"SUCCEEDED", "CANCELLED"}:
            return job
        await self._admin_service.save_job_result(
            job.id,
            status="RUNNING",
            provider_request_id=None,
            error_code=None,
            now=now,
        )
        if not text.strip():
            return await self._admin_service.save_job_result(
                job.id,
                status="FAILED",
                provider_request_id=None,
                error_code="TTS_TEXT_EMPTY",
                now=now,
            )
        try:
            result = await self._tts.synthesize(stable_key, voice, text)
        except Exception:
            return await self._admin_service.save_job_result(
                job.id,
                status="FAILED",
                provider_request_id=None,
                error_code="TTS_PROVIDER_FAILED",
                now=now,
            )
        current = await self._admin_service.get_job(job.id)
        if current.status == "CANCELLED":
            return current
        try:
            validate_object_key(result.object_key)
            if not result.object_key.startswith("generated/audio/") or result.duration_ms <= 0:
                raise AppError("TTS_OBJECT_INVALID", "TTS输出对象无效", 422)
            asset_id = await self._register_generated_audio(result, now)
            await self._admin_service.create_audio_candidate(
                stable_key=stable_key,
                target_type=target_type,
                asset_id=asset_id,
                source="TTS",
                actor_id="system",
                now=now,
                provider_request_id=result.provider_request_id,
                processing_job_id=job.id,
            )
        except Exception:
            return await self._admin_service.save_job_result(
                job.id,
                status="FAILED",
                provider_request_id=None,
                error_code="TTS_OBJECT_REGISTRATION_FAILED",
                now=now,
            )
        return await self._admin_service.save_job_result(
            job.id,
            status="SUCCEEDED",
            provider_request_id=result.provider_request_id,
            error_code=None,
            now=now,
        )


class CeleryMediaTaskDispatcher:
    def __init__(self, *, enabled: bool) -> None:
        self._enabled = enabled

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
        self._require_enabled()
        celery_app.send_task(
            "juya.content.audio.generate",
            kwargs={
                "job_id": job_id,
                "stable_key": stable_key,
                "target_type": target_type,
                "text": text,
                "voice": voice,
            },
        )


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
    if settings.environment not in {"local", "test"}:
        raise RuntimeError("local media providers are disabled outside local/test")
    if settings.database_url is None:
        raise RuntimeError("JUYA_DATABASE_URL is required for media tasks")
    database_url = settings.database_url.get_secret_value().replace(
        "mysql+pymysql://", "mysql+asyncmy://", 1
    )
    engine = create_engine(database_url)
    sessions = create_session_factory(engine)
    admin_repository = SQLAlchemyMediaAdminRepository(sessions)
    asset_repository = SQLAlchemyMediaRepository(sessions)

    async def register_generated_audio(result: TtsResult, now: datetime) -> str:
        digest = hashlib.sha256(result.object_key.encode()).hexdigest()
        asset = await asset_repository.save(
            MediaAsset(
                id=new_ulid(now),
                object_key=result.object_key,
                asset_type="audio",
                content_type="audio/mpeg",
                size=max(result.duration_ms, 1),
                sha256=digest,
                status="CONFIRMED",
                security_status="PASSED",
                created_by="system",
                created_at=now,
            )
        )
        return asset.id

    return (
        PersistentMediaTaskService(
            MediaAdminService(admin_repository),
            admin_repository,
            LocalOcrProvider(),
            LocalTtsProvider(),
            register_generated_audio=register_generated_audio,
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
