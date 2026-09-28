from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from juya_admin_api.integrations.ocr.protocol import OcrProvider, OcrResult
from juya_admin_api.integrations.tts.protocol import TtsProvider, TtsResult
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
