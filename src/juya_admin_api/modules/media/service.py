import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import parse_qsl, urlsplit

from juya_admin_api.integrations.content_security.policy import security_status_usable
from juya_admin_api.integrations.content_security.protocol import ContentSecurityProvider
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
from juya_admin_api.modules.media.inspection import inspect_media
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid

IMAGE_MAX_BYTES = 20 * 1024 * 1024
AUDIO_MAX_BYTES = 50 * 1024 * 1024
IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/bmp"}
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
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    security_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class SignedMedia:
    url: str = field(repr=False)
    expires_at: datetime


class MediaRepository(Protocol):
    async def get_by_object_key(self, object_key: str) -> MediaAsset | None: ...
    async def bind_fixed_object(self, asset: MediaAsset, object_key: str) -> MediaAsset: ...
    async def get(self, asset_id: str) -> MediaAsset | None: ...
    async def get_by_hash(self, asset_type: str, sha256: str) -> MediaAsset | None: ...

    async def save(self, asset: MediaAsset) -> MediaAsset: ...

    async def update_security(self, asset: MediaAsset) -> MediaAsset: ...


class InMemoryMediaRepository:
    def __init__(self) -> None:
        self.assets: dict[str, MediaAsset] = {}
        self.by_hash: dict[tuple[str, str], str] = {}

    async def get(self, asset_id: str) -> MediaAsset | None:
        return self.assets.get(asset_id)

    async def get_by_object_key(self, object_key: str) -> MediaAsset | None:
        return next(
            (asset for asset in self.assets.values() if asset.object_key == object_key), None
        )

    async def bind_fixed_object(self, asset: MediaAsset, object_key: str) -> MediaAsset:
        current = self.assets[asset.id]
        if current.object_key.startswith("sealed/media/"):
            return current
        current = replace(current, object_key=object_key)
        self.assets[asset.id] = current
        return current

    async def update_security(self, asset: MediaAsset) -> MediaAsset:
        self.assets[asset.id] = asset
        return asset

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
    async def create_batch_with_items(
        self, batch: BatchJob, items: list[BatchJobItem]
    ) -> BatchJob: ...

    async def claim_batch(
        self, batch_id: str, now: datetime, lease_token: str | None = None
    ) -> bool: ...
    async def heartbeat_batch(self, batch_id: str, lease_token: str, now: datetime) -> bool: ...
    async def list_recoverable_batches(self, now: datetime, limit: int = 100) -> list[str]: ...
    async def finish_claimed_batch_item(
        self,
        batch_id: str,
        item_key: str,
        lease_token: str,
        result: dict[str, object],
        error_code: str | None,
        now: datetime,
    ) -> bool: ...
    async def claim_batch_item(
        self, batch_id: str, item_key: str, now: datetime, lease_token: str | None = None
    ) -> bool: ...
    async def cancel_pending_batch_items(self, batch_id: str, now: datetime) -> None: ...

    async def claim_job(self, job_id: str, now: datetime) -> bool: ...

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

    async def transition_trash(
        self, entry_id: str, action: str, actor_id: str, now: datetime
    ) -> TrashEntry: ...


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

    async def create_batch_with_items(self, batch: BatchJob, items: list[BatchJobItem]) -> BatchJob:
        existing_id = self.batch_by_business.get(batch.business_key)
        if existing_id:
            return self.batches[existing_id]
        self.batches[batch.id] = batch
        self.batch_by_business[batch.business_key] = batch.id
        self.batch_items.update({(item.batch_id, item.item_key): item for item in items})
        return batch

    async def claim_batch(
        self, batch_id: str, now: datetime, lease_token: str | None = None
    ) -> bool:
        batch = self.batches.get(batch_id)
        if batch is None or batch.status not in {"PENDING", "RUNNING"}:
            return False
        if (
            batch.status == "RUNNING"
            and (batch.lease_expires_at or batch.updated_at + timedelta(minutes=5)) > now
        ):
            return False
        for key, item in self.batch_items.items():
            if key[0] == batch_id and item.status in {"RUNNING", "PENDING"}:
                self.batch_items[key] = replace(
                    item,
                    status=("FAILED" if item.status == "RUNNING" else "CANCELLED")
                    if batch.cancel_requested_at
                    else "PENDING",
                    error_code="BATCH_INTERRUPTED_CANCELLED"
                    if batch.cancel_requested_at and item.status == "RUNNING"
                    else None,
                    updated_at=now,
                )
        self.batches[batch_id] = replace(
            batch,
            status="CANCELLED" if batch.cancel_requested_at else "RUNNING",
            updated_at=now,
            lease_token=lease_token or new_ulid(now),
            lease_expires_at=now + timedelta(minutes=5),
            failure_count=sum(
                item.status == "FAILED"
                for (owner, _), item in self.batch_items.items()
                if owner == batch_id
            ),
            completed_at=now if batch.cancel_requested_at else None,
        )
        return True

    async def heartbeat_batch(self, batch_id: str, lease_token: str, now: datetime) -> bool:
        batch = self.batches[batch_id]
        if batch.lease_token != lease_token or batch.status != "RUNNING":
            return False
        self.batches[batch_id] = replace(batch, lease_expires_at=now + timedelta(minutes=5))
        return True

    async def list_recoverable_batches(self, now: datetime, limit: int = 100) -> list[str]:
        return [
            batch.id
            for batch in self.batches.values()
            if batch.status == "PENDING"
            or (
                batch.status == "RUNNING"
                and (batch.lease_expires_at or batch.updated_at + timedelta(minutes=5)) <= now
            )
        ][:limit]

    async def finish_claimed_batch_item(
        self,
        batch_id: str,
        item_key: str,
        lease_token: str,
        result: dict[str, object],
        error_code: str | None,
        now: datetime,
    ) -> bool:
        batch = self.batches[batch_id]
        item = self.batch_items[(batch_id, item_key)]
        if batch.lease_token != lease_token or item.status != "RUNNING":
            return False
        results = dict(batch.result_payload)
        results[item_key] = {
            "status": "FAILED" if error_code else "SUCCEEDED",
            "error_code": error_code,
            "result": result,
        }
        self.batches[batch_id] = replace(batch, result_payload=results)
        version = result.get("version")
        await MediaAdminService(self).finish_batch_item(
            batch_id,
            item_key,
            succeeded=error_code is None,
            error_code=error_code,
            result_version=version if isinstance(version, int) else None,
            now=now,
        )
        return True

    async def claim_batch_item(
        self, batch_id: str, item_key: str, now: datetime, lease_token: str | None = None
    ) -> bool:
        item = self.batch_items.get((batch_id, item_key))
        batch = self.batches.get(batch_id)
        if (
            item is None
            or item.status != "PENDING"
            or batch is None
            or batch.cancel_requested_at
            or (lease_token is not None and batch.lease_token != lease_token)
        ):
            return False
        self.batch_items[(batch_id, item_key)] = replace(
            item, status="RUNNING", attempt_count=item.attempt_count + 1, updated_at=now
        )
        return True

    async def cancel_pending_batch_items(self, batch_id: str, now: datetime) -> None:
        batch = self.batches[batch_id]
        self.batches[batch_id] = replace(batch, cancel_requested_at=now, updated_at=now)
        for key, item in self.batch_items.items():
            if key[0] == batch_id and item.status == "PENDING":
                self.batch_items[key] = replace(item, status="CANCELLED", updated_at=now)

    async def claim_job(self, job_id: str, now: datetime) -> bool:
        job = await self.get_job(job_id)
        if job is None or job.status != "PENDING":
            return False
        self.jobs[job.business_key] = replace(job, status="RUNNING", updated_at=now)
        return True

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

    async def transition_trash(
        self, entry_id: str, action: str, actor_id: str, now: datetime
    ) -> TrashEntry:
        del actor_id
        entry = self.trash_entries.get(entry_id)
        if entry is None:
            raise AppError("TRASH_ENTRY_NOT_FOUND", "回收站记录不存在", 404)
        if action == "RESTORE" and entry.status == "RESTORED":
            return entry
        if action == "CLEANUP" and entry.status == "CLEANED":
            return entry
        if entry.status != "TRASHED":
            raise AppError("TRASH_ENTRY_NOT_ACTIVE", "回收站记录当前不可操作", 409)
        if (entry.scene_id, entry.revision_id) not in self.drafts:
            raise AppError("DRAFT_NOT_TRASHABLE", "草稿已不存在或已发布", 409)
        if action == "RESTORE":
            result = replace(entry, status="RESTORED", restored_at=now)
        else:
            if now < entry.retention_until:
                raise AppError("TRASH_RETENTION_ACTIVE", "草稿仍在 30 天保留期内", 409)
            if entry.revision_id in self.referenced_drafts:
                raise AppError("DRAFT_REFERENCED", "草稿仍被其他对象引用", 409)
            self.drafts.discard((entry.scene_id, entry.revision_id))
            result = replace(entry, status="CLEANED", cleaned_at=now)
        self.trash_entries[entry_id] = result
        return result

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
        input_payload: dict[str, object] | None = None,
    ) -> BatchJob:
        existing = await self._repository.get_batch_by_business_key(business_key)
        if existing is not None:
            return existing
        if not 1 <= len(target_ids) <= 500:
            raise AppError("BATCH_SIZE_INVALID", "批量任务必须包含 1 至 500 项", 422)
        batch = BatchJob(
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
            input_payload=dict(input_payload or {}),
        )
        items = [
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
            for index, target_id in enumerate(target_ids)
        ]
        return await self._repository.create_batch_with_items(batch, items)

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
                attempt_count=max(item.attempt_count, 1),
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
            status = (
                "CANCELLED"
                if batch.cancel_requested_at
                else "COMPLETED"
                if failure_count == 0
                else "COMPLETED_WITH_ERRORS"
            )
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
        await self._repository.cancel_pending_batch_items(batch_id, now)
        batch = await self.get_batch(batch_id)
        items = await self._repository.list_batch_items(batch_id)
        saved = replace(
            batch,
            status="RUNNING" if any(item.status == "RUNNING" for item in items) else "CANCELLED",
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

    async def create_audio_target(self, stable_key: str, target_type: str) -> AudioTarget:
        if target_type not in {"scene", "vocabulary", "chunk", "SCENE", "VOCABULARY", "CHUNK"}:
            raise AppError("AUDIO_TARGET_TYPE_INVALID", "音频目标类型无效", 422)
        existing = await self._repository.get_audio_target_by_stable_key(stable_key)
        if existing is not None:
            if existing.target_type.lower() != target_type.lower():
                raise AppError("AUDIO_TARGET_MISMATCH", "音频目标类型不匹配", 409)
            return existing
        return await self._repository.save_audio_target(
            AudioTarget(new_ulid(datetime.now(UTC)), stable_key, target_type.lower(), None)
        )

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
        return await self._repository.transition_trash(entry_id, "RESTORE", actor_id, now)

    async def cleanup_draft(self, entry_id: str, *, actor_id: str, now: datetime) -> TrashEntry:
        return await self._repository.transition_trash(entry_id, "CLEANUP", actor_id, now)


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
        security: ContentSecurityProvider | None = None,
        ffprobe_path: str = "ffprobe",
        require_review: bool = True,
    ) -> None:
        self._oss = oss
        self._repository = repository
        self._signed_url_ttl_seconds = signed_url_ttl_seconds
        self._security = security
        self._ffprobe_path = ffprobe_path
        self._require_review = require_review

    async def get_asset(self, asset_id: str) -> MediaAsset:
        asset = await self._repository.get(asset_id)
        if asset is None:
            raise AppError("MEDIA_ASSET_NOT_FOUND", "媒体素材不存在", 404)
        if asset.status != "CONFIRMED" or not security_status_usable(
            asset.security_status, require_review=self._require_review
        ):
            raise AppError("MEDIA_ASSET_UNAVAILABLE", "媒体素材未通过检查", 409)
        return await self.ensure_fixed_asset(asset)

    async def ensure_fixed_asset(self, asset: MediaAsset) -> MediaAsset:
        if asset.object_key.startswith("sealed/media/"):
            return asset
        data = await self.read_asset_bytes(asset)
        fixed = await self._oss.freeze_bytes(data, asset.asset_type, asset.content_type)
        return await self._repository.bind_fixed_object(asset, fixed)

    async def read_asset_bytes(self, asset: MediaAsset) -> bytes:
        data = await self._oss.read_bytes(asset.object_key, self._max_bytes(asset.asset_type))
        inspected = await inspect_media(data, asset.asset_type, self._ffprobe_path)
        if inspected.sha256 != asset.sha256:
            raise AppError("MEDIA_ASSET_CHANGED", "素材字节已变化, 请重新上传确认", 409)
        return data

    async def create_upload_policy(self, asset_type: str, actor_id: str) -> UploadPolicy:
        max_bytes = self._max_bytes(asset_type)
        prefix = self._prefix(asset_type, actor_id) + new_ulid(datetime.now(UTC)) + "/"
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
        data = await self._oss.read_bytes(object_key, self._max_bytes(asset_type))
        if not data or len(data) > self._max_bytes(asset_type):
            raise AppError("MEDIA_SIZE_INVALID", "素材大小不符合要求", 422)
        inspected = await inspect_media(data, asset_type, self._ffprobe_path)
        existing = await self._repository.get_by_hash(asset_type, inspected.sha256)
        if self._security is None:
            raise AppError("MEDIA_SECURITY_UNAVAILABLE", "未配置独立内容安全检查", 503)
        if existing is not None:
            existing = await self.ensure_fixed_asset(existing)
        if (
            existing is not None
            and existing.status == "CONFIRMED"
            and security_status_usable(
                existing.security_status, require_review=self._require_review
            )
            and (existing.security_request_id or existing.security_status == "SKIPPED")
            and existing.width == inspected.width
            and existing.height == inspected.height
            and existing.duration_ms == inspected.duration_ms
        ):
            return existing
        object_key = (
            existing.object_key
            if existing
            else await self._oss.freeze_bytes(data, asset_type, inspected.content_type)
        )
        if asset_type == "images":
            security = await self._security.scan_image(object_key)
        elif existing is not None and existing.security_request_id:
            security = await self._security.poll_audio(existing.security_request_id)
        else:
            security = await self._security.scan_audio(object_key)
        if existing is not None:
            updated = replace(
                existing,
                content_type=inspected.content_type,
                size=inspected.size,
                width=inspected.width,
                height=inspected.height,
                duration_ms=inspected.duration_ms,
                security_status=security.status,
                status="CONFIRMED"
                if security_status_usable(security.status, require_review=self._require_review)
                else "PENDING",
                security_request_id=security.provider_request_id or None,
            )
            await self._repository.update_security(updated)
            if security.status == "BLOCKED":
                raise AppError("MEDIA_SECURITY_BLOCKED", "素材未通过安全检查", 422)
            return updated
        if security.status != "PENDING" and not security_status_usable(
            security.status, require_review=self._require_review
        ):
            raise AppError("MEDIA_SECURITY_BLOCKED", "素材未通过安全检查", 422)
        return await self._repository.save(
            MediaAsset(
                new_ulid(now),
                object_key,
                asset_type,
                inspected.content_type,
                inspected.size,
                inspected.sha256,
                "CONFIRMED"
                if security_status_usable(security.status, require_review=self._require_review)
                else "PENDING",
                security.status,
                actor_id,
                now,
                inspected.width,
                inspected.height,
                inspected.duration_ms,
                security.provider_request_id or None,
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
        if object_key.startswith(("uploads/images/", "uploads/audio/", "generated/audio/")):
            asset = await self._repository.get_by_object_key(object_key)
            if asset is None:
                raise AppError("MEDIA_ASSET_UNAVAILABLE", "未确认的教学素材不能签名", 409)
            object_key = (await self.get_asset(asset.id)).object_key
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
        if (
            not security_status_usable(security_status, require_review=self._require_review)
            or deleted_at is not None
        ):
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
        if asset_type == "audio" and not any(
            metadata.object_key.lower().endswith(suffix) for suffix in AUDIO_SUFFIXES
        ):
            raise AppError("MEDIA_EXTENSION_INVALID", "音频扩展名不支持", 422)
