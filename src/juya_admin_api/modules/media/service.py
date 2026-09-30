import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import parse_qsl, urlsplit

from juya_admin_api.integrations.oss.provider import ObjectMetadata, OssProvider, UploadPolicy
from juya_admin_api.modules.media.domain import (
    AudioTarget,
    AudioVersion,
    BatchJob,
    BatchJobItem,
    OcrCandidate,
    ProcessingJob,
    TrashEntry,
)
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid

IMAGE_MAX_BYTES = 20 * 1024 * 1024
AUDIO_MAX_BYTES = 50 * 1024 * 1024
IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
AUDIO_MIME_TYPES = {
    "audio/mpeg",
    "audio/mp4",
    "audio/x-m4a",
    "audio/wav",
    "audio/x-wav",
    "audio/aac",
}
AUDIO_SUFFIXES = {".mp3", ".m4a", ".wav", ".aac"}


@dataclass(frozen=True, slots=True)
class MediaAsset:
    id: str
    object_key: str
    asset_type: str
    content_type: str
    size: int
    sha256: str
    status: str
    security_status: str
    created_by: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SignedMedia:
    url: str = field(repr=False)
    expires_at: datetime


class MediaRepository(Protocol):
    async def get_by_hash(self, asset_type: str, sha256: str) -> MediaAsset | None: ...

    async def save(self, asset: MediaAsset) -> MediaAsset: ...


class InMemoryMediaRepository:
    def __init__(self) -> None:
        self.assets: dict[str, MediaAsset] = {}
        self.by_hash: dict[tuple[str, str], str] = {}

    async def get_by_hash(self, asset_type: str, sha256: str) -> MediaAsset | None:
        asset_id = self.by_hash.get((asset_type, sha256))
        return self.assets.get(asset_id) if asset_id else None

    async def save(self, asset: MediaAsset) -> MediaAsset:
        existing = await self.get_by_hash(asset.asset_type, asset.sha256)
        if existing is not None:
            return existing
        self.assets[asset.id] = asset
        self.by_hash[(asset.asset_type, asset.sha256)] = asset.id
        return asset


class MediaAdminRepository(Protocol):
    async def get_job_by_business_key(self, business_key: str) -> ProcessingJob | None: ...

    async def get_job(self, job_id: str) -> ProcessingJob | None: ...

    async def save_job(self, job: ProcessingJob) -> ProcessingJob: ...

    async def get_ocr_candidate_by_job(self, job_id: str) -> OcrCandidate | None: ...

    async def save_ocr_candidate(self, candidate: OcrCandidate) -> OcrCandidate: ...

    async def get_batch_by_business_key(self, business_key: str) -> BatchJob | None: ...

    async def get_batch(self, batch_id: str) -> BatchJob | None: ...

    async def list_batches(self) -> list[BatchJob]: ...

    async def save_batch(self, batch: BatchJob) -> BatchJob: ...

    async def list_batch_items(self, batch_id: str) -> list[BatchJobItem]: ...

    async def save_batch_item(self, item: BatchJobItem) -> BatchJobItem: ...

    async def get_audio_target(self, target_id: str) -> AudioTarget | None: ...

    async def get_audio_target_by_stable_key(self, stable_key: str) -> AudioTarget | None: ...

    async def save_audio_target(self, target: AudioTarget) -> AudioTarget: ...

    async def list_audio_targets(self) -> list[AudioTarget]: ...

    async def get_audio_version(self, version_id: str) -> AudioVersion | None: ...

    async def find_audio_version_by_provider_request(
        self, provider_request_id: str
    ) -> AudioVersion | None: ...

    async def save_audio_version(self, version: AudioVersion) -> AudioVersion: ...

    async def list_audio_versions(self, target_id: str) -> list[AudioVersion]: ...

    async def get_trash_entry(self, entry_id: str) -> TrashEntry | None: ...

    async def get_trash_by_revision(self, revision_id: str) -> TrashEntry | None: ...

    async def save_trash_entry(self, entry: TrashEntry) -> TrashEntry: ...

    async def list_trash_entries(self) -> list[TrashEntry]: ...

    async def is_draft_revision(self, scene_id: str, revision_id: str) -> bool: ...

    async def has_draft_references(self, revision_id: str) -> bool: ...

    async def purge_draft(self, scene_id: str, revision_id: str) -> None: ...


class InMemoryMediaAdminRepository:
    def __init__(self) -> None:
        self.jobs: dict[str, ProcessingJob] = {}
        self.ocr_candidates: dict[str, OcrCandidate] = {}
        self.batches: dict[str, BatchJob] = {}
        self.batch_by_business: dict[str, str] = {}
        self.batch_items: dict[tuple[str, str], BatchJobItem] = {}
        self.audio_targets: dict[str, AudioTarget] = {}
        self.audio_target_by_stable_key: dict[str, str] = {}
        self.audio_versions: dict[str, AudioVersion] = {}
        self.audio_version_by_provider_request: dict[str, str] = {}
        self.trash_entries: dict[str, TrashEntry] = {}
        self.trash_by_revision: dict[str, str] = {}
        self.drafts: set[tuple[str, str]] = set()
        self.referenced_drafts: set[str] = set()

    async def get_job_by_business_key(self, business_key: str) -> ProcessingJob | None:
        return self.jobs.get(business_key)

    async def get_job(self, job_id: str) -> ProcessingJob | None:
        return next((job for job in self.jobs.values() if job.id == job_id), None)

    async def save_job(self, job: ProcessingJob) -> ProcessingJob:
        existing = self.jobs.get(job.business_key)
        if existing is not None and existing.id != job.id:
            return existing
        self.jobs[job.business_key] = job
        return job

    async def get_ocr_candidate_by_job(self, job_id: str) -> OcrCandidate | None:
        return self.ocr_candidates.get(job_id)

    async def save_ocr_candidate(self, candidate: OcrCandidate) -> OcrCandidate:
        existing = self.ocr_candidates.get(candidate.job_id)
        if existing is not None and existing.id != candidate.id:
            return existing
        self.ocr_candidates[candidate.job_id] = candidate
        return candidate

    async def get_batch_by_business_key(self, business_key: str) -> BatchJob | None:
        batch_id = self.batch_by_business.get(business_key)
        return self.batches.get(batch_id) if batch_id else None

    async def get_batch(self, batch_id: str) -> BatchJob | None:
        return self.batches.get(batch_id)

    async def list_batches(self) -> list[BatchJob]:
        return sorted(self.batches.values(), key=lambda batch: batch.created_at, reverse=True)

    async def save_batch(self, batch: BatchJob) -> BatchJob:
        existing_id = self.batch_by_business.get(batch.business_key)
        if existing_id is not None and existing_id != batch.id:
            return self.batches[existing_id]
        self.batches[batch.id] = batch
        self.batch_by_business[batch.business_key] = batch.id
        return batch

    async def list_batch_items(self, batch_id: str) -> list[BatchJobItem]:
        return sorted(
            (item for (owner, _), item in self.batch_items.items() if owner == batch_id),
            key=lambda item: item.item_key,
        )

    async def save_batch_item(self, item: BatchJobItem) -> BatchJobItem:
        self.batch_items[(item.batch_id, item.item_key)] = item
        return item

    async def get_audio_target(self, target_id: str) -> AudioTarget | None:
        return self.audio_targets.get(target_id)

    async def get_audio_target_by_stable_key(self, stable_key: str) -> AudioTarget | None:
        target_id = self.audio_target_by_stable_key.get(stable_key)
        return self.audio_targets.get(target_id) if target_id else None

    async def save_audio_target(self, target: AudioTarget) -> AudioTarget:
        existing_id = self.audio_target_by_stable_key.get(target.stable_key)
        if existing_id is not None and existing_id != target.id:
            return self.audio_targets[existing_id]
        self.audio_targets[target.id] = target
        self.audio_target_by_stable_key[target.stable_key] = target.id
        return target

    async def list_audio_targets(self) -> list[AudioTarget]:
        return sorted(self.audio_targets.values(), key=lambda target: target.stable_key)

    async def get_audio_version(self, version_id: str) -> AudioVersion | None:
        return self.audio_versions.get(version_id)

    async def find_audio_version_by_provider_request(
        self, provider_request_id: str
    ) -> AudioVersion | None:
        version_id = self.audio_version_by_provider_request.get(provider_request_id)
        return self.audio_versions.get(version_id) if version_id else None

    async def save_audio_version(self, version: AudioVersion) -> AudioVersion:
        if version.provider_request_id:
            existing_id = self.audio_version_by_provider_request.get(version.provider_request_id)
            if existing_id is not None and existing_id != version.id:
                return self.audio_versions[existing_id]
            self.audio_version_by_provider_request[version.provider_request_id] = version.id
        self.audio_versions[version.id] = version
        return version

    async def list_audio_versions(self, target_id: str) -> list[AudioVersion]:
        return sorted(
            (version for version in self.audio_versions.values() if version.target_id == target_id),
            key=lambda version: version.version_no,
        )

    async def get_trash_entry(self, entry_id: str) -> TrashEntry | None:
        return self.trash_entries.get(entry_id)

    async def get_trash_by_revision(self, revision_id: str) -> TrashEntry | None:
        entry_id = self.trash_by_revision.get(revision_id)
        return self.trash_entries.get(entry_id) if entry_id else None

    async def save_trash_entry(self, entry: TrashEntry) -> TrashEntry:
        existing_id = self.trash_by_revision.get(entry.revision_id)
        if existing_id is not None and existing_id != entry.id:
            return self.trash_entries[existing_id]
        self.trash_entries[entry.id] = entry
        self.trash_by_revision[entry.revision_id] = entry.id
        return entry

    async def list_trash_entries(self) -> list[TrashEntry]:
        return sorted(self.trash_entries.values(), key=lambda entry: entry.trashed_at, reverse=True)

    async def is_draft_revision(self, scene_id: str, revision_id: str) -> bool:
        return (scene_id, revision_id) in self.drafts

    async def has_draft_references(self, revision_id: str) -> bool:
        return revision_id in self.referenced_drafts

    async def purge_draft(self, scene_id: str, revision_id: str) -> None:
        self.drafts.discard((scene_id, revision_id))

    def register_draft(self, scene_id: str, revision_id: str) -> None:
        self.drafts.add((scene_id, revision_id))

    def set_draft_referenced(self, revision_id: str, referenced: bool) -> None:
        if referenced:
            self.referenced_drafts.add(revision_id)
        else:
            self.referenced_drafts.discard(revision_id)


class MediaAdminService:
    def __init__(self, repository: MediaAdminRepository) -> None:
        self._repository = repository

    async def create_job(
        self,
        *,
        business_key: str,
        job_type: str,
        target_id: str,
        actor_id: str,
        now: datetime,
        batch_id: str | None = None,
        input_payload: dict[str, object] | None = None,
    ) -> ProcessingJob:
        existing = await self._repository.get_job_by_business_key(business_key)
        if existing is not None:
            return existing
        return await self._repository.save_job(
            ProcessingJob(
                id=new_ulid(now),
                business_key=business_key,
                job_type=job_type,
                target_id=target_id,
                batch_id=batch_id,
                status="PENDING",
                provider_request_id=None,
                error_code=None,
                created_by=actor_id,
                created_at=now,
                updated_at=now,
                input_payload=dict(input_payload or {}),
            )
        )

    async def get_job(self, job_id: str) -> ProcessingJob:
        job = await self._repository.get_job(job_id)
        if job is None:
            raise AppError("MEDIA_JOB_NOT_FOUND", "媒体任务不存在", 404)
        return job

    async def save_job_result(
        self,
        job_id: str,
        *,
        status: str,
        provider_request_id: str | None,
        error_code: str | None,
        now: datetime,
    ) -> ProcessingJob:
        job = await self.get_job(job_id)
        if job.status in {"SUCCEEDED", "CANCELLED"}:
            return job
        return await self._repository.save_job(
            replace(
                job,
                status=status,
                provider_request_id=provider_request_id,
                error_code=error_code,
                updated_at=now,
            )
        )

    async def cancel_job(self, job_id: str, *, now: datetime) -> ProcessingJob:
        job = await self.get_job(job_id)
        if job.status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return job
        return await self._repository.save_job(
            replace(job, status="CANCELLED", cancel_requested_at=now, updated_at=now)
        )

    async def record_ocr_candidate(
        self,
        job_id: str,
        *,
        provider_request_id: str,
        text_value: str,
        blocks: list[dict[str, object]],
        template_type: str,
        now: datetime,
    ) -> OcrCandidate:
        existing = await self._repository.get_ocr_candidate_by_job(job_id)
        if existing is not None:
            return existing
        job = await self.get_job(job_id)
        return await self._repository.save_ocr_candidate(
            OcrCandidate(
                id=new_ulid(now),
                job_id=job.id,
                asset_id=job.target_id,
                business_key=job.business_key,
                provider_request_id=provider_request_id,
                status="READY",
                template_type=template_type,
                structured_candidate={"text": text_value, "blocks": blocks},
                confidence=_candidate_confidence(blocks),
                error_code=None,
                created_at=now,
            )
        )

    async def get_ocr_candidate(self, job_id: str) -> OcrCandidate:
        await self.get_job(job_id)
        candidate = await self._repository.get_ocr_candidate_by_job(job_id)
        if candidate is None:
            raise AppError("OCR_CANDIDATE_NOT_FOUND", "OCR 候选尚未生成", 404)
        return candidate

    async def confirm_ocr_candidate(
        self,
        job_id: str,
        revision_id: str,
        *,
        actor_id: str,
        now: datetime,
    ) -> OcrCandidate:
        candidate = await self.get_ocr_candidate(job_id)
        if candidate.confirmed_revision_id is not None:
            return candidate
        return await self._repository.save_ocr_candidate(
            replace(
                candidate,
                status="CONFIRMED",
                confirmed_revision_id=revision_id,
                confirmed_by=actor_id,
                confirmed_at=now,
            )
        )

    async def create_batch(
        self,
        *,
        business_key: str,
        job_type: str,
        target_ids: tuple[str, ...],
        actor_id: str,
        now: datetime,
    ) -> BatchJob:
        existing = await self._repository.get_batch_by_business_key(business_key)
        if existing is not None:
            return existing
        if not 1 <= len(target_ids) <= 500:
            raise AppError("BATCH_SIZE_INVALID", "批量任务必须包含 1 至 500 项", 422)
        batch = await self._repository.save_batch(
            BatchJob(
                id=new_ulid(now),
                business_key=business_key,
                job_type=job_type,
                status="PENDING",
                total_count=len(target_ids),
                success_count=0,
                failure_count=0,
                created_by=actor_id,
                created_at=now,
                updated_at=now,
            )
        )
        for index, target_id in enumerate(target_ids):
            await self._repository.save_batch_item(
                BatchJobItem(
                    id=new_ulid(now),
                    batch_id=batch.id,
                    item_key=f"{index}:{target_id}",
                    target_id=target_id,
                    status="PENDING",
                    attempt_count=0,
                    error_code=None,
                    result_version=None,
                    updated_at=now,
                )
            )
        return batch

    async def get_batch(self, batch_id: str) -> BatchJob:
        batch = await self._repository.get_batch(batch_id)
        if batch is None:
            raise AppError("BATCH_JOB_NOT_FOUND", "批量任务不存在", 404)
        return batch

    async def list_batches(self) -> list[BatchJob]:
        return await self._repository.list_batches()

    async def list_batch_items(self, batch_id: str) -> list[BatchJobItem]:
        await self.get_batch(batch_id)
        return await self._repository.list_batch_items(batch_id)

    async def finish_batch_item(
        self,
        batch_id: str,
        item_key: str,
        *,
        succeeded: bool,
        error_code: str | None,
        result_version: int | None,
        now: datetime,
    ) -> BatchJobItem:
        batch = await self.get_batch(batch_id)
        items = await self._repository.list_batch_items(batch_id)
        item = next((candidate for candidate in items if candidate.item_key == item_key), None)
        if item is None:
            raise AppError("BATCH_ITEM_NOT_FOUND", "批量任务项不存在", 404)
        if item.status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return item
        saved = await self._repository.save_batch_item(
            replace(
                item,
                status="SUCCEEDED" if succeeded else "FAILED",
                attempt_count=item.attempt_count + 1,
                error_code=None if succeeded else error_code,
                result_version=result_version,
                updated_at=now,
            )
        )
        items = await self._repository.list_batch_items(batch_id)
        success_count = sum(candidate.status == "SUCCEEDED" for candidate in items)
        failure_count = sum(candidate.status == "FAILED" for candidate in items)
        terminal_count = sum(
            candidate.status in {"SUCCEEDED", "FAILED", "CANCELLED"} for candidate in items
        )
        if terminal_count == batch.total_count:
            status = "COMPLETED" if failure_count == 0 else "COMPLETED_WITH_ERRORS"
            completed_at = now
        else:
            status = "RUNNING"
            completed_at = None
        await self._repository.save_batch(
            replace(
                batch,
                status=status,
                success_count=success_count,
                failure_count=failure_count,
                updated_at=now,
                completed_at=completed_at,
            )
        )
        return saved

    async def cancel_batch(self, batch_id: str, *, now: datetime) -> BatchJob:
        batch = await self.get_batch(batch_id)
        if batch.status in {"COMPLETED", "COMPLETED_WITH_ERRORS", "CANCELLED"}:
            return batch
        for item in await self._repository.list_batch_items(batch_id):
            if item.status in {"PENDING", "RUNNING"}:
                await self._repository.save_batch_item(
                    replace(item, status="CANCELLED", updated_at=now)
                )
        items = await self._repository.list_batch_items(batch_id)
        saved = replace(
            batch,
            status="CANCELLED",
            success_count=sum(item.status == "SUCCEEDED" for item in items),
            failure_count=sum(item.status == "FAILED" for item in items),
            updated_at=now,
            completed_at=now,
            cancel_requested_at=now,
        )
        return await self._repository.save_batch(saved)

    async def create_audio_candidate(
        self,
        *,
        stable_key: str,
        target_type: str,
        asset_id: str,
        source: str,
        actor_id: str,
        now: datetime,
        provider_request_id: str | None = None,
        processing_job_id: str | None = None,
    ) -> AudioVersion:
        if source not in {"MANUAL", "TTS"}:
            raise AppError("AUDIO_SOURCE_INVALID", "音频来源不正确", 422)
        if provider_request_id:
            existing = await self._repository.find_audio_version_by_provider_request(
                provider_request_id
            )
            if existing is not None:
                return existing
        target = await self._repository.get_audio_target_by_stable_key(stable_key)
        if target is None:
            target = await self._repository.save_audio_target(
                AudioTarget(new_ulid(now), stable_key, target_type, None)
            )
        versions = await self._repository.list_audio_versions(target.id)
        version = AudioVersion(
            id=new_ulid(now),
            target_id=target.id,
            asset_id=asset_id,
            version_no=max((candidate.version_no for candidate in versions), default=0) + 1,
            source=source,
            status="CANDIDATE",
            provider_request_id=provider_request_id,
            processing_job_id=processing_job_id,
            created_by=actor_id,
            created_at=now,
        )
        return await self._repository.save_audio_version(version)

    async def get_audio_target(self, target_id: str) -> AudioTarget:
        target = await self._repository.get_audio_target(target_id)
        if target is None:
            raise AppError("AUDIO_TARGET_NOT_FOUND", "音频目标不存在", 404)
        return target

    async def list_audio_targets(self) -> list[AudioTarget]:
        return await self._repository.list_audio_targets()

    async def list_audio_versions(self, target_id: str) -> list[AudioVersion]:
        await self.get_audio_target(target_id)
        return await self._repository.list_audio_versions(target_id)

    async def confirm_audio_version(
        self, version_id: str, *, actor_id: str, now: datetime
    ) -> AudioTarget:
        del actor_id, now
        version = await self._repository.get_audio_version(version_id)
        if version is None:
            raise AppError("AUDIO_VERSION_NOT_FOUND", "音频版本不存在", 404)
        target = await self.get_audio_target(version.target_id)
        for candidate in await self._repository.list_audio_versions(target.id):
            desired_status = "ACTIVE" if candidate.id == version.id else "SUPERSEDED"
            if candidate.status != desired_status:
                await self._repository.save_audio_version(replace(candidate, status=desired_status))
        target = replace(target, active_version_id=version.id)
        return await self._repository.save_audio_target(target)

    async def rollback_audio_version(
        self,
        target_id: str,
        version_id: str,
        *,
        actor_id: str,
        now: datetime,
    ) -> AudioTarget:
        target = await self.get_audio_target(target_id)
        version = await self._repository.get_audio_version(version_id)
        if version is None or version.target_id != target.id:
            raise AppError("AUDIO_VERSION_NOT_FOUND", "音频版本不存在", 404)
        return await self.confirm_audio_version(version_id, actor_id=actor_id, now=now)

    async def trash_draft(
        self,
        scene_id: str,
        revision_id: str,
        *,
        actor_id: str,
        now: datetime,
    ) -> TrashEntry:
        existing = await self._repository.get_trash_by_revision(revision_id)
        if existing is not None:
            return existing
        if not await self._repository.is_draft_revision(scene_id, revision_id):
            raise AppError("DRAFT_NOT_TRASHABLE", "只有未发布草稿可进入回收站", 409)
        return await self._repository.save_trash_entry(
            TrashEntry(
                id=new_ulid(now),
                scene_id=scene_id,
                revision_id=revision_id,
                status="TRASHED",
                trashed_by=actor_id,
                trashed_at=now,
                retention_until=now + timedelta(days=30),
            )
        )

    async def get_trash_entry(self, entry_id: str) -> TrashEntry:
        entry = await self._repository.get_trash_entry(entry_id)
        if entry is None:
            raise AppError("TRASH_ENTRY_NOT_FOUND", "回收站记录不存在", 404)
        return entry

    async def list_trash_entries(self) -> list[TrashEntry]:
        return await self._repository.list_trash_entries()

    async def restore_draft(self, entry_id: str, *, actor_id: str, now: datetime) -> TrashEntry:
        del actor_id
        entry = await self.get_trash_entry(entry_id)
        if entry.status != "TRASHED":
            return entry
        return await self._repository.save_trash_entry(
            replace(entry, status="RESTORED", restored_at=now)
        )

    async def cleanup_draft(self, entry_id: str, *, actor_id: str, now: datetime) -> TrashEntry:
        del actor_id
        entry = await self.get_trash_entry(entry_id)
        if entry.status == "CLEANED":
            return entry
        if entry.status != "TRASHED":
            raise AppError("TRASH_ENTRY_NOT_ACTIVE", "回收站记录当前不可清理", 409)
        if now < entry.retention_until:
            raise AppError("TRASH_RETENTION_ACTIVE", "草稿仍在 30 天保留期内", 409)
        if await self._repository.has_draft_references(entry.revision_id):
            raise AppError("DRAFT_REFERENCED", "草稿仍被其他对象引用", 409)
        await self._repository.purge_draft(entry.scene_id, entry.revision_id)
        return await self._repository.save_trash_entry(
            replace(entry, status="CLEANED", cleaned_at=now)
        )


def _candidate_confidence(blocks: list[dict[str, object]]) -> float | None:
    values = [
        float(value)
        for block in blocks
        if isinstance((value := block.get("confidence")), (int, float))
    ]
    return sum(values) / len(values) if values else None


class MediaService:
    def __init__(
        self,
        oss: OssProvider,
        repository: MediaRepository,
        *,
        signed_url_ttl_seconds: int = 300,
    ) -> None:
        self._oss = oss
        self._repository = repository
        self._signed_url_ttl_seconds = signed_url_ttl_seconds

    async def create_upload_policy(self, asset_type: str, actor_id: str) -> UploadPolicy:
        max_bytes = self._max_bytes(asset_type)
        prefix = self._prefix(asset_type, actor_id)
        return await self._oss.create_upload_policy(prefix, max_bytes, 600)

    async def confirm_upload(
        self,
        asset_type: str,
        actor_id: str,
        object_key: str,
        now: datetime,
    ) -> MediaAsset:
        prefix = self._prefix(asset_type, actor_id)
        if not object_key.startswith(prefix) or ".." in object_key:
            raise AppError("MEDIA_OBJECT_KEY_INVALID", "素材对象键无效", 422)
        metadata = await self._oss.head_object(object_key)
        self._validate_metadata(asset_type, metadata)
        existing = await self._repository.get_by_hash(asset_type, metadata.sha256)
        if existing is not None:
            return existing
        return await self._repository.save(
            MediaAsset(
                new_ulid(now),
                object_key,
                asset_type,
                metadata.content_type,
                metadata.size,
                metadata.sha256,
                "CONFIRMED",
                "PASSED",
                actor_id,
                now,
            )
        )

    def validate_batch(self, asset_type: str, count: int) -> None:
        limit = 30 if asset_type == "images" else 300 if asset_type == "audio" else 0
        if count < 1 or count > limit:
            raise AppError("MEDIA_BATCH_LIMIT", "素材批次数量超限", 422)

    async def sign_media(
        self,
        object_key: str,
        entitlement_expires_at: datetime | None,
        now: datetime,
    ) -> SignedMedia:
        ttl = self._signed_url_ttl_seconds
        if entitlement_expires_at is not None:
            expires_at = (
                entitlement_expires_at
                if entitlement_expires_at.tzinfo
                else entitlement_expires_at.replace(tzinfo=UTC)
            )
            ttl = min(ttl, int((expires_at - now).total_seconds()))
        if ttl <= 0:
            raise AppError("MEDIA_ACCESS_EXPIRED", "媒体访问权限已到期", 403)
        url = await self._oss.sign_get_url(object_key, ttl)
        expires_at = now + timedelta(seconds=ttl)
        query = dict(parse_qsl(urlsplit(url).query))
        # V4 may be shorter than requested when the server uses expiring STS credentials.
        if "x-oss-date" in query and "x-oss-expires" in query:
            try:
                signed_at = datetime.strptime(query["x-oss-date"], "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=UTC
                )
                signature_expiry = signed_at + timedelta(seconds=int(query["x-oss-expires"]))
            except (ValueError, OverflowError):
                raise AppError("OSS_SIGNATURE_INVALID", "OSS签名时间无效", 503) from None
            expires_at = min(expires_at, signature_expiry)
        return SignedMedia(url, expires_at)

    async def sign_feedback_screenshot(
        self,
        object_key: str,
        security_status: str,
        deleted_at: datetime | None,
        now: datetime,
    ) -> SignedMedia:
        if not object_key or ".." in object_key:
            raise AppError("FEEDBACK_SCREENSHOT_INVALID", "反馈截图对象键无效", 422)
        if security_status != "PASSED" or deleted_at is not None:
            raise AppError("FEEDBACK_SCREENSHOT_UNAVAILABLE", "反馈截图当前不可访问", 409)
        return await self.sign_media(object_key, None, now)

    @staticmethod
    def _prefix(asset_type: str, actor_id: str) -> str:
        if asset_type not in {"images", "audio"}:
            raise AppError("MEDIA_TYPE_INVALID", "素材类型无效", 422)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", actor_id):
            raise AppError("MEDIA_ACTOR_INVALID", "上传者标识无效", 422)
        return f"uploads/{asset_type}/{actor_id}/"

    @staticmethod
    def _max_bytes(asset_type: str) -> int:
        if asset_type == "images":
            return IMAGE_MAX_BYTES
        if asset_type == "audio":
            return AUDIO_MAX_BYTES
        raise AppError("MEDIA_TYPE_INVALID", "素材类型无效", 422)

    @staticmethod
    def _validate_metadata(asset_type: str, metadata: ObjectMetadata) -> None:
        allowed_mime = IMAGE_MIME_TYPES if asset_type == "images" else AUDIO_MIME_TYPES
        max_bytes = IMAGE_MAX_BYTES if asset_type == "images" else AUDIO_MAX_BYTES
        if metadata.content_type.lower() not in allowed_mime:
            raise AppError("MEDIA_MIME_INVALID", "素材格式不支持", 422)
        if metadata.size <= 0 or metadata.size > max_bytes:
            raise AppError("MEDIA_SIZE_INVALID", "素材大小不符合要求", 422)
        if not re.fullmatch(r"[0-9a-fA-F]{64}", metadata.sha256):
            raise AppError("MEDIA_HASH_INVALID", "素材哈希无效", 422)
        if metadata.metadata.get("decodable") != "true":
            raise AppError("MEDIA_DECODE_FAILED", "素材无法解码", 422)
        if metadata.metadata.get("security_status") != "PASSED":
            raise AppError("MEDIA_SECURITY_BLOCKED", "素材未通过安全检查", 422)
        if asset_type == "audio" and not any(
            metadata.object_key.lower().endswith(suffix) for suffix in AUDIO_SUFFIXES
        ):
            raise AppError("MEDIA_EXTENSION_INVALID", "音频扩展名不支持", 422)
