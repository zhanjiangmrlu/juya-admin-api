from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ProcessingJob:
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
    cancel_requested_at: datetime | None = None
    input_payload: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OcrCandidate:
    id: str
    job_id: str
    asset_id: str
    business_key: str
    provider_request_id: str | None
    status: str
    template_type: str
    structured_candidate: dict[str, object]
    confidence: float | None
    error_code: str | None
    created_at: datetime
    confirmed_revision_id: str | None = None
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class BatchJob:
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
    completed_at: datetime | None = None
    cancel_requested_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class BatchJobItem:
    id: str
    batch_id: str
    item_key: str
    target_id: str
    status: str
    attempt_count: int
    error_code: str | None
    result_version: int | None
    updated_at: datetime
    processing_job_id: str | None = None


@dataclass(frozen=True, slots=True)
class AudioTarget:
    id: str
    stable_key: str
    target_type: str
    active_version_id: str | None


@dataclass(frozen=True, slots=True)
class AudioVersion:
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


@dataclass(frozen=True, slots=True)
class TrashEntry:
    id: str
    scene_id: str
    revision_id: str
    status: str
    trashed_by: str
    trashed_at: datetime
    retention_until: datetime
    restored_at: datetime | None = None
    cleaned_at: datetime | None = None
